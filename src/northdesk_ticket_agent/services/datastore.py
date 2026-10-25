import json
import re
from importlib import resources
from typing import Any

from northdesk_ticket_agent.domain.models import Customer, Invoice, KbArticle, Ticket

_WORD = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    "a an and are as at be but by can do for from has have how i in is it its my no not "
    "of on or our so the this to us was we what when why with you your".split()
)


def _words(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _STOPWORDS}


class DataStore:
    """Read-only access to customers, invoices and knowledge base articles."""

    def __init__(
        self,
        customers: list[Customer],
        invoices: list[Invoice],
        articles: list[KbArticle],
    ) -> None:
        self._customers = {c.customer_id: c for c in customers}
        self._invoices = invoices
        self._articles = articles
        self._article_words = [(_words(a.title), _words(a.text)) for a in articles]

    @classmethod
    def from_package_data(cls) -> DataStore:
        return cls(
            customers=[Customer(**row) for row in _load("customers.json")],
            invoices=[Invoice(**row) for row in _load("invoices.json")],
            articles=[KbArticle(**row) for row in _load("kb.json")],
        )

    def get_customer(self, customer_id: str) -> Customer | None:
        return self._customers.get(customer_id)

    def get_invoices(self, customer_id: str) -> list[Invoice]:
        return [inv for inv in self._invoices if inv.customer_id == customer_id]

    def article_ids(self) -> set[str]:
        return {a.article_id for a in self._articles}

    def search_kb(self, query: str, limit: int = 3) -> list[KbArticle]:
        """Rank articles by how many query words appear in their title and text."""
        query_words = _words(query)
        scored = []
        for article, (title_words, text_words) in zip(self._articles, self._article_words, strict=True):
            title_hits = len(query_words & title_words)
            text_hits = len(query_words & text_words)
            score = 2 * title_hits + text_hits
            if score:
                scored.append((score, article))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [article for _, article in scored[:limit]]


def load_example_tickets() -> list[Ticket]:
    return [Ticket(**row) for row in _load("tickets.json")]


def _load(name: str) -> list[dict[str, Any]]:
    text = resources.files("northdesk_ticket_agent.data").joinpath(name).read_text("utf-8")
    rows: list[dict[str, Any]] = json.loads(text)
    return rows
