# `master.db` schema

`schema.sql` is the DDL of a real Rekordbox 7 library — 47 tables and 288
indexes — used by the Rekordbox **exporter** to create a database from nothing.

**It contains no user data.** Table and index definitions only, extracted with
`SELECT sql FROM sqlite_master`.

## Why this, and not pyrekordbox's models

`Base.metadata.create_all()` looked like the obvious way to build the schema, and
it is wrong in two ways that matter:

- it models **37 tables**, not 47;
- it marks columns **NOT NULL that Rekordbox leaves nullable**. `agentRegistry
  .id_1` is the one that caught it: a real library has NULL there, and a database
  built from the ORM refuses the row Rekordbox itself writes.

So the DDL is the authority and the models are not. Regenerate with the snippet
in the export handoff if a future Rekordbox version changes the schema.
