from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection

from chat.embeddings import embed_texts, embedding_source_text, embedding_text_hash
from chat.models import ContentEmbedding, UniversityContent


class Command(BaseCommand):
    help = "Build or refresh pgvector embeddings for active university content."

    def add_arguments(self, parser):
        parser.add_argument("--batch-size", type=int, default=32)
        parser.add_argument("--limit", type=int, default=0)
        parser.add_argument("--force", action="store_true")

    def handle(self, *args, **options):
        if connection.vendor != "postgresql":
            self.stdout.write(self.style.WARNING("Skipping embeddings: pgvector requires PostgreSQL."))
            return

        batch_size = max(1, options["batch_size"])
        limit = max(0, options["limit"])
        model_name = settings.EMBEDDING_MODEL

        queryset = UniversityContent.objects.filter(is_active=True).order_by("id")
        if limit:
            queryset = queryset[:limit]

        processed = 0
        updated = 0
        batch: list[UniversityContent] = []

        def flush(items: list[UniversityContent]) -> None:
            nonlocal processed, updated
            if not items:
                return

            prepared: list[tuple[UniversityContent, str, str]] = []
            for item in items:
                text = embedding_source_text(item.title, item.content)
                text_hash = embedding_text_hash(text)
                processed += 1
                if not options["force"]:
                    existing = getattr(item, "embedding_record", None)
                    if (
                        existing
                        and existing.model_name == model_name
                        and existing.text_hash == text_hash
                    ):
                        continue
                prepared.append((item, text, text_hash))

            if not prepared:
                return

            vectors = embed_texts([entry[1] for entry in prepared], model_name=model_name)
            for (item, _text, text_hash), vector in zip(prepared, vectors):
                ContentEmbedding.objects.update_or_create(
                    content=item,
                    defaults={
                        "model_name": model_name,
                        "embedding": vector,
                        "text_hash": text_hash,
                    },
                )
                updated += 1

            self.stdout.write(f"Processed={processed} updated={updated}")

        for record in queryset.iterator(chunk_size=batch_size):
            batch.append(record)
            if len(batch) >= batch_size:
                flush(batch)
                batch = []
        flush(batch)

        self.stdout.write(self.style.SUCCESS(f"Embeddings ready. processed={processed} updated={updated}"))
