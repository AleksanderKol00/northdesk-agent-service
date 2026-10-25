from fastapi import FastAPI

app = FastAPI(
    title="Northdesk Ticket Agent",
    description=(
        "Triages Northdesk support tickets: an LLM agent proposes an action, code checks it, "
        "and a Temporal workflow carries it out, retrying when a service fails for a short time."
    ),
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
