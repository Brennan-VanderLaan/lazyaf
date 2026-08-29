# In-container control runtime. Lives at /control inside lazyaf-base; imported
# as the `control` package by tdd/unit/control_runtime via a sys.path insert of
# images/base. Imports NOTHING from backend/app — the wire contract with the
# backend is pinned by tdd/unit/control_runtime/test_config_contract.py.
