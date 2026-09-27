"""RTDP contract registry: loading, validation, and digests.

Contracts are versioned semantic definitions (signal contracts, feature
definitions, rulesets, provider bindings, action policies, state flows)
stored as YAML under contracts/ and seed assets under assets/.
"""

from .digest import canonical_digest  # noqa: F401
from .registry import ContractRegistry, ContractError  # noqa: F401
