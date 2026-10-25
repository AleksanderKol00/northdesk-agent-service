from fastapi import FastAPI

app = FastAPI(
    title="Northdesk Ticket Agent",
    description=(
        "Processes Northdesk support tickets in several steps with an AI agent, "
        "and keeps working when the AI or other services fail temporarily."
    ),
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
