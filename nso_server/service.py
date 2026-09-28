"""Concrete room server assembled from focused behavior mixins."""

from .mixins.combat import CombatMixin
from .mixins.commands import CommandsMixin
from .mixins.core import CoreMixin
from .mixins.interaction_clan import ClanInteractionMixin
from .mixins.interaction_combat import CombatInteractionMixin
from .mixins.interaction_gameplay import GameplayInteractionMixin
from .mixins.interaction_party import PartyInteractionMixin
from .mixins.interaction_trade import TradeInteractionMixin
from .mixins.interactions import InteractionsMixin
from .mixins.lifecycle import LifecycleMixin
from .mixins.persistence import PersistenceMixin
from .mixins.social import SocialMixin


class RoomServer(
    CommandsMixin,
    InteractionsMixin,
    GameplayInteractionMixin,
    ClanInteractionMixin,
    CombatInteractionMixin,
    PartyInteractionMixin,
    TradeInteractionMixin,
    LifecycleMixin,
    CombatMixin,
    SocialMixin,
    PersistenceMixin,
    CoreMixin,
):
    """Async NSO room relay with behavior grouped by domain."""

