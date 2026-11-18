"""People's record ids: ``<prefix>_<ulid>``.

The ids ``org_<ulid>`` / ``ct_<ulid>`` / ``ix_<ulid>`` and the rest are the **public
contract** other tools join on. Each is minted by the SDK's one minter
(:func:`nimblebrain_server_sdk.new_prefixed_id`): opaque, never recycled, and sortable by mint
time, so ids minted in one batch keep their order. The prefix names the entity; the ULID is
the opaque body.
"""

from __future__ import annotations

from nimblebrain_server_sdk import new_prefixed_id, new_ulid

__all__ = [
    "CONTACT_PREFIX",
    "EDGE_PREFIX",
    "EXTERNAL_REF_PREFIX",
    "FIELD_DEF_PREFIX",
    "INTERACTION_PREFIX",
    "OPPORTUNITY_PREFIX",
    "ORG_PREFIX",
    "PIPELINE_STAGE_PREFIX",
    "PROOF_POINT_PREFIX",
    "SIGNAL_PREFIX",
    "SIGNAL_TYPE_PREFIX",
    "new_contact_id",
    "new_external_ref_id",
    "new_field_def_id",
    "new_interaction_id",
    "new_opportunity_id",
    "new_organization_edge_id",
    "new_organization_id",
    "new_pipeline_stage_id",
    "new_proof_point_id",
    "new_signal_id",
    "new_signal_type_id",
    "new_ulid",
]

# Entity prefixes. Opaque body follows the underscore.
ORG_PREFIX = "org"
CONTACT_PREFIX = "ct"
INTERACTION_PREFIX = "ix"
EDGE_PREFIX = "oe"  # organization_edge (the org↔org graph edge)
OPPORTUNITY_PREFIX = "op"  # opportunity (the pipeline entity)
SIGNAL_PREFIX = "sg"  # signal (an attested fact about an account)
PROOF_POINT_PREFIX = "pp"  # proof_point (an attested claim about our own work)
FIELD_DEF_PREFIX = "fd"  # field_def (a custom field's definition)
PIPELINE_STAGE_PREFIX = "ps"  # pipeline_stage (one stage of a workspace's pipeline)
SIGNAL_TYPE_PREFIX = "st"  # signal_type (one key of a workspace's signal vocabulary)
EXTERNAL_REF_PREFIX = "xr"  # external_ref (another system's URI for a record)


def new_organization_id() -> str:
    """``org_<ulid>`` — opaque Organization id."""
    return new_prefixed_id(ORG_PREFIX)


def new_contact_id() -> str:
    """``ct_<ulid>`` — opaque Contact id."""
    return new_prefixed_id(CONTACT_PREFIX)


def new_interaction_id() -> str:
    """``ix_<ulid>`` — opaque Interaction id."""
    return new_prefixed_id(INTERACTION_PREFIX)


def new_organization_edge_id() -> str:
    """``oe_<ulid>`` — opaque org↔org graph edge id."""
    return new_prefixed_id(EDGE_PREFIX)


def new_opportunity_id() -> str:
    """``op_<ulid>`` — opaque Opportunity id."""
    return new_prefixed_id(OPPORTUNITY_PREFIX)


def new_signal_id() -> str:
    """``sg_<ulid>`` — opaque Signal id."""
    return new_prefixed_id(SIGNAL_PREFIX)


def new_proof_point_id() -> str:
    """``pp_<ulid>`` — opaque ProofPoint id."""
    return new_prefixed_id(PROOF_POINT_PREFIX)


def new_field_def_id() -> str:
    """``fd_<ulid>`` — opaque FieldDef id."""
    return new_prefixed_id(FIELD_DEF_PREFIX)


def new_pipeline_stage_id() -> str:
    """``ps_<ulid>`` — opaque PipelineStage id."""
    return new_prefixed_id(PIPELINE_STAGE_PREFIX)


def new_signal_type_id() -> str:
    """``st_<ulid>`` — opaque SignalType id."""
    return new_prefixed_id(SIGNAL_TYPE_PREFIX)


def new_external_ref_id() -> str:
    """``xr_<ulid>`` — opaque ExternalRef row id (internal; a reference is its URI)."""
    return new_prefixed_id(EXTERNAL_REF_PREFIX)
