from .across import AcrossResolver
from .base import CrossChainLink, CrossChainResolver, recipient_from_destination
from .layerzero import LayerZeroResolver
from .thorchain import ThorchainResolver
from .wormhole import WormholeResolver

RESOLVERS = {
    "thorchain": ThorchainResolver,
    "wormhole": WormholeResolver,
    "layerzero": LayerZeroResolver,
    "across": AcrossResolver,
}

__all__ = ["RESOLVERS", "AcrossResolver", "CrossChainLink", "CrossChainResolver", "LayerZeroResolver", "ThorchainResolver", "WormholeResolver", "recipient_from_destination"]
