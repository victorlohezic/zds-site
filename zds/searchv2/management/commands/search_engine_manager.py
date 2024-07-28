from django.core.management.base import BaseCommand, CommandError
from django.conf import settings

from zds.searchv2.utils import SearchIndexManager, get_all_indexable_classes


class Command(BaseCommand):
    help = "Index data in Typesense and manage them"

    search_engine_manager = None

    def add_arguments(self, parser):
        parser.add_argument(
            "action",
            type=str,
            help="action to perform (clear: remove everything, setup: clear + create schemes, index_all: setup + index everything, index_flagged: index only what is required)",
            choices=["setup", "clear", "index_all", "index_flagged"],
        )
        parser.add_argument("-q", "--quiet", action="store_true", default=False)

    def handle(self, *args, **options):
        self.search_engine_manager = SearchIndexManager()

        if not self.search_engine_manager.connected:
            raise Exception("Unable to connect to the search engine, aborting.")

        if options["action"] == "setup":
            self.search_engine_manager.reset_index()
        elif options["action"] == "clear":
            self.search_engine_manager.clear_index()
        elif options["action"] == "index_all":
            self.index_documents(force_reindexing=True, quiet=options["quiet"])
        elif options["action"] == "index_flagged":
            self.index_documents(force_reindexing=False, quiet=options["quiet"])
        else:
            raise CommandError("unknown action {}".format(options["action"]))

    def index_documents(self, force_reindexing=False, quiet=False):
        verbose = not quiet

        if force_reindexing:
            self.search_engine_manager.reset_index()  # remove all previous data and create schemes

        for model in get_all_indexable_classes(only_models=True):
            # Models take care of indexing classes that are not models
            if verbose:
                self.stdout.write(f"- indexing {model.get_search_document_type()}s")

            indexed_counter = self.search_engine_manager.indexing_of_model(
                model, force_reindexing=force_reindexing, verbose=verbose
            )

            if verbose:
                self.stdout.write(f"  {indexed_counter}\titems indexed")
