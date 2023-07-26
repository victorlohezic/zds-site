from functools import partial
import logging
import time
from datetime import datetime

from django.apps import apps
from django.db import models
from django.conf import settings

from django.db import transaction
from typesense import Client as SearchEngineClient
from bs4 import BeautifulSoup
import re


def document_indexer(obj):
    return obj.get_document_for_indexing()


class AbstractSearchIndexable:
    """Mixin for indexable objects.

    Define a number of different functions that can be overridden to tune the behavior of indexing into the search_engine (typesense).

    You (may) need to override :

    - ``get_indexable()`` ;
    - ``get_schema()`` (not mandatory, but otherwise, the search engine will choose the schema by itself) ;
    - ``get_document()`` (not mandatory, but may be useful if data differ from schema or extra stuffs need to be done).

    You also need to maintain ``search_engine_id`` and ``search_engine_already_indexed`` for indexing (if any).
    """

    search_engine_already_indexed = False
    search_engine_id = ""

    objects_per_batch = 100

    @classmethod
    def get_document_type(cls):
        """value of the ``_type`` field in the index"""
        content_type = cls.__name__.lower()

        # fetch parents
        for base in cls.__bases__:
            if issubclass(base, AbstractSearchIndexable) and base != AbstractSearchIndexableModel:
                content_type = base.__name__.lower() + "_" + content_type

        return content_type

    @classmethod
    def get_document_schema(self):
        """Setup schema for the model(data scheme).

        See https://typesense.org/docs/0.23.0/api/collections.html#with-pre-defined-schema

        .. attention::
            You *may* want to override this method (otherwise the search engine choose the schema by itself).

        :return: schema object.  A dictionary containing the name, fields of the collection.
        :rtype: dict
        """
        search_engine_schema = dict()
        search_engine_schema["name"] = self.get_document_type()
        search_engine_schema["fields"] = [{"name": ".*", "type": "auto"}]
        return search_engine_schema

    @classmethod
    def get_indexable(cls, force_reindexing=False):
        """Return objects to index.

        .. attention::
            You need to override this method (otherwise nothing will be indexed).

        :param force_reindexing: force to return all objects, even if they may already be indexed.
        :type force_reindexing: bool
        :rtype: list
        """

        return []

    def get_document_source(self, excluded_fields=None):
        """Create a document from the variable of the class, based on the schema.

        .. attention::
            You may need to override this method if the data differ from the schema for some reason.

        :param excluded_fields: exclude some field from the default method
        :type excluded_fields: list
        :return: document
        :rtype: dict
        """

        cls = self.__class__
        schema = cls.get_document_schema()["fields"]
        fields = list(schema[i]["name"] for i in range(len(schema)))

        data = {}

        for field in fields:
            if excluded_fields and field in excluded_fields:
                data[field] = None
                continue

            v = getattr(self, field, None)
            if callable(v):
                v = v()

            data[field] = v

        return data

    def get_document_for_indexing(self, action="index"):
        """Create a document formatted for indexing.

        See https://typesense.org/docs/0.19.0/api/documents.html#index-a-document

        :return: the document
        :rtype: dict
        """

        document = self.get_document_source()
        document["id"] = self.search_engine_id

        return document


class AbstractSearchIndexableModel(AbstractSearchIndexable, models.Model):
    """Version of AbstractSearchIndexable for a Django object, with some improvements :

    - Already include ``pk`` in schema ;
    - Match the search engine ``_id`` field and ``pk`` ;
    - Override ``search_engine_already_indexed`` to a database field.
    - Define a ``search_engine_flagged`` field to restrict the number of object to be indexed ;
    - Override ``save()`` to manage the field ;
    - Define a ``get_indexable_objects()`` method that can be overridden to change the queryset to fetch object.
    """

    class Meta:
        abstract = True

    search_engine_flagged = models.BooleanField(
        "Doit être (ré)indexé par le moteur de recherche", default=True, db_index=True
    )
    search_engine_already_indexed = models.BooleanField(
        "Déjà indexé par le moteur de recherche", default=False, db_index=True
    )

    def __init__(self, *args, **kwargs):
        """Override to match the search engine ``_id`` field and ``pk``"""
        super().__init__(*args, **kwargs)
        self.search_engine_id = str(self.pk)

    @classmethod
    def get_indexable_objects(cls, force_reindexing=False):
        """Method that can be overridden to filter django objects from database based on any criterion.

        :param force_reindexing: force to return all objects, even if they may be already indexed.
        :type force_reindexing: bool
        :return: query
        :rtype: django.db.models.query.QuerySet
        """

        query = cls.objects

        if not force_reindexing:
            query = query.filter(search_engine_flagged=True)

        return query

    @classmethod
    def get_indexable(cls, force_reindexing=False):
        """Override ``get_indexable()`` in order to use the Django querysets and batch objects.

        :return: a queryset
        :rtype: django.db.models.query.QuerySet
        """

        return cls.get_indexable_objects(force_reindexing).order_by("pk").all()

    def save(self, *args, **kwargs):
        """Override the ``save()`` method to flag the object if saved
        (which assumes a modification of the object, so the need to reindex).

        .. note::
            Flagging can be prevented using ``save(search_engine_flagged=False)``.
        """

        self.search_flagged = kwargs.pop("search_engine_flagged", True)

        return super().save(*args, **kwargs)


def date_to_timestamp_int(date):
    """Converts a given datetime object to Unix timestamp.
    The purpose of this function is for indexing datetime objects in Typesense.

    :param date: the datetime object to be converted
    :type date: datetime.datetime

    :return: the Unix timestamp corresponding to the given datetime object
    :rtype: int
    """
    return int(datetime.timestamp(date))


def clean_html(text):
    """Removes all HTML tags from the given text using BeautifulSoup.

    :param text: the text to be cleaned
    :type text: str

    :return: the cleaned text with all HTML tags removed
    :rtype: str
    """
    result = ""
    if text != None:
        soup = BeautifulSoup(text, "html.parser")
        formatted_html = soup.prettify()
        result = re.sub(r"<[^>]*>", "", formatted_html).strip()
    return result


def delete_document_in_search_engine(instance):
    """Delete a AbstractSearchIndexable from the search engine database.
    Must be implemented by all classes that derive from AbstractSearchIndexableModel.

    :param instance: the document to delete
    :type instance: AbstractSearchIndexable
    """

    search_engine_manager = SearchIndexManager()

    search_engine_manager.delete_document(instance)


def get_all_indexable_objects():
    """Return all indexable objects registered in Django"""
    return [model for model in apps.get_models() if issubclass(model, AbstractSearchIndexableModel)]


class SearchIndexManager:
    """Manage a given index with different taylor-made functions"""

    def __init__(self, connection_alias="default"):
        """Create a manager for a given index


        :param connection_alias: the alias for connection
        :type connection_alias: str
        """

        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

        self.search_engine = None
        self.connected_to_search_engine = False

        if settings.SEARCH_ENABLED:
            self.search_engine = SearchEngineClient(settings.SEARCH_CONNECTIONS[connection_alias])
            self.connected_to_search_engine = True

            # test connection:
            try:
                self.search_engine.api_call.get("/health")
            except:
                self.connected_to_search_engine = False
                self.logger.warn("failed to connect to Typesense")
            else:
                self.logger.info("connected to Typesense")

    def clear_index(self):
        """Clear index"""

        if not self.connected_to_search_engine:
            return

        collections = {collection["name"] for collection in self.search_engine.collections.retrieve()}
        for collection in collections:
            self.search_engine.collections[collection].delete()

        self.logger.info(f"index cleared, {len(collections)} collections deleted")

    def reset_index(self, models):
        """Delete old collections and create new ones.
        Then, set schemas for the different models.

        :param models: list of models
        :type models: list
        """

        if not self.connected_to_search_engine:
            return

        self.clear_index()
        models = set(models)  # avoid duplicates
        for model in models:
            schema = model.get_document_schema()
            self.search_engine.collections.create(schema)

        self.logger.info("index created")

    def clear_indexing_of_model(self, model):
        """Nullify the indexing of a given model by setting ``search_engine_already_index=False`` to all objects.

        Use full updating for ``AbstractSearchIndexableModel``, instead of saving all of them.

        :param model: the model
        :type model: class
        """

        if issubclass(model, AbstractSearchIndexableModel):  # use a global update with Django
            objs = model.get_indexable_objects(force_reindexing=True)
            objs.update(search_engine_flagged=True, search_engine_already_indexed=False)
        else:
            for objects in model.get_indexable(force_reindexing=True):
                for obj in objects:
                    obj.search_engine_already_indexed = False

        self.logger.info(f"unindex {model.get_document_type()}")

    def indexing_of_model(self, model, force_reindexing=False):
        """Index documents of a given model. Use the ``objects_per_batch`` property to index.

        See https://typesense.org/docs/0.23.0/api/documents.html#index-multiple-documents

        .. attention::
            + Currently only working with ``AbstractSearchIndexableModel``.

        :param model: and model
        :type model: class
        :param force_reindexing: force all document to be returned
        :type force_reindexing: bool
        :return: the number of documents indexed
        :rtype: int
        """

        if not self.connected_to_search_engine:
            return

        # better safe than sorry
        if model.__name__ == "FakeChapter":
            self.logger.warn("Cannot index FakeChapter model. Please index its parent model.")
            return 0

        documents_formatter = partial(document_indexer)
        objects_per_batch = getattr(model, "objects_per_batch", 100)
        indexed_counter = 0
        if model.__name__ == "PublishedContent":
            generate = model.get_indexable(force_reindexing)
            while True:
                with transaction.atomic():
                    try:
                        # fetch a batch
                        objects = next(generate)
                    except StopIteration:
                        break
                    if not objects:
                        break
                    if hasattr(objects[0], "parent_id"):
                        model_to_update = objects[0].parent_model
                        pks = [o.parent_id for o in objects]
                        doc_type = "chapter"
                    else:
                        model_to_update = model
                        pks = [o.pk for o in objects]
                        doc_type = model.get_document_type()

                    formatted_documents = list(map(documents_formatter, objects))

                    self.search_engine.collections[doc_type].documents.import_(
                        formatted_documents, {"action": "create"}
                    )
                    for document in formatted_documents:
                        if self.logger.getEffectiveLevel() <= logging.INFO:
                            self.logger.info("{} {} with id {}".format("index", doc_type, document["id"]))

                    # mark all these objects as indexed at once
                    model_to_update.objects.filter(pk__in=pks).update(
                        search_engine_already_indexed=True, search_engine_flagged=False
                    )
                    indexed_counter += len(objects)
            return indexed_counter
        else:
            then = time.time()
            prev_obj_per_sec = False
            last_pk = 0
            object_source = model.get_indexable(force_reindexing)

            while True:
                with transaction.atomic():
                    # fetch a batch
                    objects = list(object_source.filter(pk__gt=last_pk)[:objects_per_batch])

                    if not objects:
                        break

                    formatted_documents = list(map(documents_formatter, objects))
                    doc_type = model.get_document_type()

                    self.search_engine.collections[doc_type].documents.import_(
                        formatted_documents, {"action": "create"}
                    )
                    for document in formatted_documents:
                        if self.logger.getEffectiveLevel() <= logging.INFO:
                            self.logger.info("{} {} with id {}".format("index", doc_type, document["id"]))

                    # mark all these objects as indexed at once
                    model.objects.filter(pk__in=[o.pk for o in objects]).update(
                        search_engine_already_indexed=True, search_engine_flagged=False
                    )
                    indexed_counter += len(objects)

                    # basic estimation of indexed objects per second
                    now = time.time()
                    last_batch_duration = int(now - then) or 1
                    then = now
                    obj_per_sec = round(float(objects_per_batch) / last_batch_duration, 2)
                    if force_reindexing:
                        print(
                            "    {} so far ({} obj/s, batch size: {})".format(
                                indexed_counter, obj_per_sec, objects_per_batch
                            )
                        )

                    if prev_obj_per_sec is False:
                        prev_obj_per_sec = obj_per_sec
                    else:
                        ratio = obj_per_sec / prev_obj_per_sec
                        # if we processed this batch 10% slower/faster than the previous one,
                        # shrink/increase batch size
                        if abs(1 - ratio) > 0.1:
                            objects_per_batch = int(objects_per_batch * ratio)
                            if force_reindexing:
                                print(f"     {round(ratio, 2)}x, new batch size: {objects_per_batch}")
                        prev_obj_per_sec = obj_per_sec

                    # fetch next batch
                    last_pk = objects[-1].pk

            return indexed_counter

    def update_single_document(self, document, doc):
        """Update given fields of a single document.

        See https://typesense.org/docs/0.23.0/api/documents.html#update-a-document

        :param document: the document
        :type document: AbstractSearchIndexable
        :param doc: fields to update
        :type doc: dict
        """

        if not self.connected_to_search_engine:
            return

        doc_type = document.get_document_type()
        doc_id = document.search_engine_id
        try:
            self.search_engine.collections[doc_type].documents[doc_id].update(doc)
            self.logger.info(f"partial_update {document.get_document_type()} with id {document.search_engine_id}")
        except:
            pass

    def delete_document(self, document):
        """Delete a given document, based on its ``search_engine_id``

        :param document: the document
        :type document: AbstractSearchIndexable
        """

        if not self.connected_to_search_engine:
            return

        doc_type = document.get_document_type()
        doc_id = document.search_engine_id
        try:
            self.search_engine.collections[doc_type].documents[doc_id].delete()
            self.logger.info(f"delete {document.get_document_type()} with id {document.search_engine_id}")
        except:
            pass

    def delete_by_query(self, doc_type="", query={"filter_by": ""}):
        """Delete a bunch of documents that match a specific filter_by condition.

        See https://typesense.org/docs/0.23.0/api/documents.html#delete-by-query

        .. attention ::
            Call to this function must be done with great care!

        :param doc_type: the document type
        :type doc_type: str
        :param query: the query to match all document to be deleted
        :type query: search request with filter_by in the search parameters
        """

        if not self.connected_to_search_engine:
            return

        response = self.search_engine.collections[doc_type].documents.delete(query)

        self.logger.info(f"delete_by_query {doc_type}s ({response})")

    def setup_search(self, request):
        """Setup search
        :param request: a string, the search request
        :type request: dictionary
        :return: formated search
        """
        if not self.connected_to_search_engine:
            return

        search_requests = {"searches": []}

        for collection in self.search_engine.collections.retrieve():
            search_requests["searches"].append({"collection": collection["name"], "q": request})
        return self.search_engine.multi_search.perform(search_requests, None)["results"]
