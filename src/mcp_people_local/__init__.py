"""The local runtime for Agent Ready CRM: one principal and one SQLite file on the user's machine.

Select it with ``PEOPLE_RUNTIME=local``, or run ``agentreadycrm``, which selects it itself.
Isolation is by installation: the store runs with exactly one principal,
``Scope("local", "local")``, so no query needs an account predicate.
"""
