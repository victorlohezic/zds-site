from copy import deepcopy

from django.conf import settings
from django.test import TestCase
from django.test.utils import override_settings

from zds.forum.tests.factories import TopicFactory, PostFactory, Topic, Post
from zds.forum.tests.factories import create_category_and_forum
from zds.member.tests.factories import ProfileFactory, StaffProfileFactory
from zds.searchv2.models import SearchIndexManager
from zds.tutorialv2.tests.factories import PublishableContentFactory, ContainerFactory, ExtractFactory, publish_content
from zds.tutorialv2.models.database import PublishedContent, FakeChapter, PublishableContent
from zds.tutorialv2.tests import TutorialTestMixin, override_for_contents


overridden_zds_app = deepcopy(settings.ZDS_APP)
overridden_zds_app["content"]["extra_content_generation_policy"] = "NONE"
overridden_zds_app["content"]["repo_private_path"] = settings.BASE_DIR / "contents-private-test"
overridden_zds_app["content"]["repo_public_path"] = settings.BASE_DIR / "contents-public-test"


@override_settings(ZDS_APP=overridden_zds_app)
@override_for_contents(SEARCH_ENABLED=True)
class SearchIndexManagerTests(TutorialTestMixin, TestCase):
    def setUp(self):
        settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
        self.mas = ProfileFactory().user
        settings.ZDS_APP["member"]["bot_account"] = self.mas.username

        self.category, self.forum = create_category_and_forum()

        self.user = ProfileFactory().user
        self.staff = StaffProfileFactory().user

        self.manager = SearchIndexManager()
        self.indexable = [FakeChapter, PublishedContent, Topic, Post]

        self.manager.reset_index(self.indexable)

    def test_setup_functions(self):
        """Test the behavior of the reset_index() and clear_index() functions"""

        if not self.manager.connected_to_search_engine:
            return

        # 1. Creation:
        models = [Topic, Post]
        self.manager.reset_index(models)

        # test collection
        collections_name = [collection["name"] for collection in self.manager.search_engine.collections.retrieve()]
        for model in models:
            self.assertTrue(model.get_document_type() in collections_name)

        # 2. Clearing
        self.manager.clear_index()
        self.assertTrue(len(self.manager.search_engine.collections.retrieve()) == 0)  # back to the void

    def test_indexation(self):
        """test the indexation and deletion of the different documents"""

        if not self.manager.connected_to_search_engine:
            return

        # create a topic with a post
        topic = TopicFactory(forum=self.forum, author=self.user)
        post = PostFactory(topic=topic, author=self.user, position=1)

        topic = Topic.objects.get(pk=topic.pk)
        post = Post.objects.get(pk=post.pk)

        self.assertTrue(topic.search_engine_requires_index)
        self.assertTrue(post.search_engine_requires_index)

        # create a middle-tutorial and publish it
        tuto = PublishableContentFactory(type="TUTORIAL")
        tuto.authors.add(self.user)
        tuto.save()

        tuto_draft = tuto.load_version()
        chapter1 = ContainerFactory(parent=tuto_draft, db_object=tuto)
        ExtractFactory(container=chapter1, db_object=tuto)
        published = publish_content(tuto, tuto_draft, is_major_update=True)

        tuto.sha_public = tuto_draft.current_version
        tuto.sha_draft = tuto_draft.current_version
        tuto.public_version = published
        tuto.save()

        published = PublishedContent.objects.get(content_pk=tuto.pk)
        self.assertTrue(published.search_engine_requires_index)

        # 1. index all
        for model in self.indexable:
            if model is FakeChapter:
                continue
            self.manager.indexing_of_model(model, force_reindexing=False)

        topic = Topic.objects.get(pk=topic.pk)
        post = Post.objects.get(pk=post.pk)

        self.assertFalse(topic.search_engine_requires_index)
        self.assertFalse(post.search_engine_requires_index)

        published = PublishedContent.objects.get(content_pk=tuto.pk)
        self.assertFalse(published.search_engine_requires_index)

        results = self.manager.search("*")  # get all documents
        number_of_results = sum(result["found"] for result in results)
        self.assertEqual(number_of_results, 4)  # get 4 results, one of each type

        must_contain = {"post": False, "topic": False, "publishedcontent": False, "chapter": False}
        id_must_be = {
            "post": str(post.pk),
            "topic": str(topic.pk),
            "publishedcontent": str(published.pk),
            "chapter": tuto.slug + "__" + chapter1.slug,
        }

        for result in results:
            doc_type = result["request_params"]["collection_name"]
            must_contain[doc_type] = True
            for hit in result["hits"]:
                doc_id = hit["document"]["id"]
                self.assertEqual(doc_id, id_must_be[doc_type])

        self.assertTrue(all(must_contain))

        # 2. Test what reindexation will do:
        new_topic = TopicFactory(forum=self.forum, author=self.user)
        new_post = PostFactory(topic=new_topic, author=self.user, position=1)

        pk_of_topics_to_reindex = []
        for item in Topic.get_indexable(force_reindexing=False):
            pk_of_topics_to_reindex.append(item.pk)

        pk_of_posts_to_reindex = []
        for item in Post.get_indexable(force_reindexing=False):
            pk_of_posts_to_reindex.append(item.pk)

        self.assertTrue(topic.pk not in pk_of_topics_to_reindex)
        self.assertTrue(new_topic.pk in pk_of_topics_to_reindex)
        self.assertTrue(post.pk not in pk_of_posts_to_reindex)
        self.assertTrue(new_post.pk in pk_of_posts_to_reindex)

        for model in self.indexable:  # ok, so let's index that
            if model is FakeChapter:
                continue
            self.manager.indexing_of_model(model, force_reindexing=False)

        results = self.manager.search("*")
        number_of_results = sum(result["found"] for result in results)
        self.assertEqual(number_of_results, 6)  # good!

        # 3. Test single deletion:
        new_post = Post.objects.get(pk=new_post.pk)

        self.manager.delete_document(new_post)

        results = self.manager.search("*")
        number_of_results = sum(result["found"] for result in results)
        self.assertEqual(number_of_results, 5)  # one is missing

        for result in results:
            doc_type = result["request_params"]["collection_name"]
            for hit in result["hits"]:
                doc_id = hit["document"]["id"]
                self.assertTrue(doc_type != Post.get_document_type() or doc_id != new_post.search_engine_id)

        # 4. Test "delete_by_query_deletion":
        topic = Topic.objects.get(pk=topic.pk)
        new_topic = Topic.objects.get(pk=new_topic.pk)

        self.manager.delete_by_query(
            Topic.get_document_type(), {"filter_by": f"id:= [{topic.search_engine_id}, {new_topic.search_engine_id}]"}
        )  # the two topic are deleted

        results = self.manager.search("*")
        number_of_results = sum(result["found"] for result in results)
        self.assertEqual(number_of_results, 3)

        for result in results:
            doc_type = result["request_params"]["collection_name"]
            for hit in result["hits"]:
                doc_id = hit["document"]["id"]
                self.assertTrue(doc_type != Topic.get_document_type() or doc_id != new_topic.search_engine_id)
                self.assertTrue(doc_type != Topic.get_document_type() or doc_id != topic.search_engine_id)

        # 5. Test that the deletion of an object also triggers its deletion in Typesense
        post = Post.objects.get(pk=post.pk)
        post.delete()

        results = self.manager.search("*")
        number_of_results = sum(result["found"] for result in results)
        self.assertEqual(number_of_results, 2)

        for result in results:
            doc_type = result["request_params"]["collection_name"]
            for hit in result["hits"]:
                doc_id = hit["document"]["id"]
                self.assertTrue(doc_type != Post.get_document_type() or doc_id != post.search_engine_id)

        # 6. Test full desindexation:
        for model in self.indexable:
            if model is FakeChapter:
                continue
            self.manager.clear_indexing_of_model(model)

        # note "topic" is gone since "post" is gone, due to relationships at the Django level
        new_topic = Topic.objects.get(pk=new_topic.pk)
        new_post = Post.objects.get(pk=new_post.pk)

        self.assertTrue(new_topic.search_engine_requires_index)
        self.assertTrue(new_post.search_engine_requires_index)

        published = PublishedContent.objects.get(content_pk=tuto.pk)
        self.assertTrue(published.search_engine_requires_index)

    def test_special_case_of_contents(self):
        """test that the old publishedcontent does not stay when a new one is created"""

        if not self.manager.connected_to_search_engine:
            return

        # 1. Create a middle-tutorial, publish it, then index it
        tuto = PublishableContentFactory(type="TUTORIAL")
        tuto.authors.add(self.user)
        tuto.save()

        tuto_draft = tuto.load_version()
        chapter1 = ContainerFactory(parent=tuto_draft, db_object=tuto)
        ExtractFactory(container=chapter1, db_object=tuto)
        published = publish_content(tuto, tuto_draft, is_major_update=True)

        tuto.sha_public = tuto_draft.current_version
        tuto.sha_draft = tuto_draft.current_version
        tuto.public_version = published
        tuto.save()

        self.manager.indexing_of_model(PublishedContent, force_reindexing=True, verbose=False)  # index

        first_publication = PublishedContent.objects.get(content_pk=tuto.pk)
        self.assertFalse(first_publication.search_engine_requires_index)

        results = self.manager.search("*")
        number_of_results = sum(result["found"] for result in results)
        self.assertEqual(number_of_results, 2)  # get 2 results, one for the content and one for the chapter

        self.assertEqual(PublishedContent.objects.count(), 1)

        # 2. Change thet title, which will trigger a change in the slug
        tuto = PublishableContent.objects.get(pk=tuto.pk)
        versioned = tuto.load_version(sha=tuto.sha_draft)

        tuto.title = "un titre complètement différent!"
        tuto.save(force_slug_update=True)

        versioned.repo_update_top_container(tuto.title, tuto.slug, "osef", "osef")
        second_publication = publish_content(tuto, versioned, True)

        tuto.sha_public = versioned.current_version
        tuto.sha_draft = versioned.current_version
        tuto.public_version = second_publication
        tuto.save()

        self.assertEqual(PublishedContent.objects.count(), 2)  # now there is two objects ...
        first_publication = PublishedContent.objects.get(pk=first_publication.pk)
        self.assertTrue(first_publication.must_redirect)  # .. including the first one, for redirection

        results = self.manager.search("*")
        number_of_results = sum(result["found"] for result in results)
        self.assertEqual(number_of_results, 0)  # the old one is gone (and we need to reindex to get the new one)

        # 3. Check if indexation brings the new one, and not the old one
        self.manager.indexing_of_model(PublishedContent, force_reindexing=True, verbose=False)  # index

        first_publication = PublishedContent.objects.get(pk=first_publication.pk)
        second_publication = PublishedContent.objects.get(pk=second_publication.pk)

        results = self.manager.search("*")
        number_of_results = sum(result["found"] for result in results)
        self.assertEqual(number_of_results, 2)  # Still 2, not 4 !

        found_old = False
        found_new = False

        for result in results:
            doc_type = result["request_params"]["collection_name"]
            for hit in result["hits"]:
                doc_id = hit["document"]["id"]
                if doc_type == PublishedContent.get_document_type():
                    if doc_id == first_publication.search_engine_id:
                        found_old = True
                    if doc_id == second_publication.search_engine_id:
                        found_new = True

        self.assertTrue(found_new)
        self.assertFalse(found_old)

    def tearDown(self):
        super().tearDown()

        # delete index:
        self.manager.clear_index()
