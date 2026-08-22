# Database migrations

Apply the current schema before starting the trader:

```bash
uv run alembic -x database_url=sqlite:///data/paper/trader.db upgrade head
```

The default URL is an in-memory SQLite database so an omitted production URL
cannot accidentally mutate a persistent database. Production startup should use
`create_session_factory(url, create_schema=False)` after migrations are applied.
