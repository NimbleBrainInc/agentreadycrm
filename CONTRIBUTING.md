# Contributing

Thanks for helping. Bug reports and pull requests are welcome on
[GitHub](https://github.com/NimbleBrainInc/agentreadycrm).

## Set up

You need [uv](https://docs.astral.sh/uv/) and Node 24.

```sh
uv sync          # the package and the dev tools
make verify      # everything CI runs
make run         # the server over stdio, on your default database
```

To try changes on a throwaway database, set `PEOPLE_LOCAL_DB=/tmp/crm-dev.db`. To preview the
views against a real server, run `cd ui && npm run dev` and open
`http://localhost:5173/__preview`; it starts the local runtime on its own database under
`ui/.preview/`.

## Pull requests

- Keep a pull request to one change, with tests for the behavior it adds or fixes.
- `make verify` must pass with no skips.
- A change to what clients see (a tool, a schema, a description, a resource) edits
  `tests/golden/surface.json` by hand in the same pull request.
- A table change adds an Alembic revision under `src/mcp_people_local/migrations/versions/`.
- This repository is public: never include internal hostnames, credentials, or names of
  private systems or customers. `make scrub` checks the tree.

`AGENTS.md` has the rules the code holds, for people and coding agents alike.

## Licence

By contributing, you agree that your contributions are licensed under the MIT licence.
