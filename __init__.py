"""GANN Python SDK."""

__version__ = "0.1.0"

from .client import (
    GannClient,
    LoadTracker,
    AgentDetails,
    AgentSearchResponse,
    AgentSchemaResponse,
    SchemaValidationError,
)

from .quic import (
    QuicOffer,
    QuicAnswer,
    QuicRelayInfo,
    QuicRelayDataFrame,
    QuicPeerServer,
    QuicPeerClient,
    QuicPeerConnection,
    QuicRelayTransport,
    connect_quic_relay_transport,
    E2eeKeyPair,
    RELAY_E2EE_ALG,
    attach_e2ee_pubkey,
    extract_e2ee_pubkey,
    encrypt_relay_payload,
    decrypt_relay_payload,
)

from .quic_session import (
    QuicDirectFirstOptions,
    QuicDirectFirstResult,
    initiate_quic_session_direct_first,
    respond_quic_offer_direct_first,
)

__all__ = [
    "GannClient",
    "LoadTracker",
    "AgentDetails",
    "AgentSearchResponse",
    "AgentSchemaResponse",
    "SchemaValidationError",
    "QuicOffer",
    "QuicAnswer",
    "QuicRelayInfo",
    "QuicRelayDataFrame",
    "QuicPeerServer",
    "QuicPeerClient",
    "QuicPeerConnection",
    "QuicRelayTransport",
    "connect_quic_relay_transport",
    "E2eeKeyPair",
    "RELAY_E2EE_ALG",
    "attach_e2ee_pubkey",
    "extract_e2ee_pubkey",
    "encrypt_relay_payload",
    "decrypt_relay_payload",
    "QuicDirectFirstOptions",
    "QuicDirectFirstResult",
    "initiate_quic_session_direct_first",
    "respond_quic_offer_direct_first",
]
