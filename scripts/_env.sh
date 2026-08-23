# Resolve the interpreter to run arteries with. Sourced by every script here;
# assumes the caller has already cd'd to the repo root.
#
# Prefer .venv, which has arteries installed editable along with rdflib and any
# other extra. Fall back to python3 with PYTHONPATH so a checkout without a venv
# still works -- but note that fallback cannot see optional extras, so
# `art ontology load` will tell you to install rdflib rather than running.
#
# CAPILLARIES_ROOT overrides the sibling-checkout assumption in both paths.

if [ -x .venv/bin/python ]; then
    PY=.venv/bin/python
else
    CAPILLARIES_SRC="${CAPILLARIES_ROOT:-../capillaries}/src"
    export PYTHONPATH="src:${CAPILLARIES_SRC}:${PYTHONPATH:-}"
    PY=python3
fi
export PY
