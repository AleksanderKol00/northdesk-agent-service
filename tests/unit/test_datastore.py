from northdesk_ticket_agent.services.datastore import DataStore


def test_get_customer(datastore: DataStore) -> None:
    customer = datastore.get_customer("C-400")
    assert customer is not None
    assert customer.status == "suspended"
    assert datastore.get_customer("C-999") is None


def test_get_invoices_only_returns_that_customers_invoices(datastore: DataStore) -> None:
    invoices = datastore.get_invoices("C-100")
    assert {inv.invoice_id for inv in invoices} == {"INV-1001", "INV-1002"}
    assert datastore.get_invoices("C-200") == []


def test_search_kb_ranks_password_article_first(datastore: DataStore) -> None:
    results = datastore.search_kb("I forgot my password")
    assert results[0].article_id == "KB-1"


def test_search_kb_returns_nothing_for_unrelated_text(datastore: DataStore) -> None:
    assert datastore.search_kb("recommend a pizza place") == []
