"""Port of StealthDesk ``src/domain/model/typer_state.h``."""

from enum import Enum


class TyperState(Enum):
    IDLE = "Idle"
    ARMED = "Armed"
    ACTIVATION_SETTLING = "ActivationSettling"
    TYPING = "Typing"
    PAUSED = "Paused"


def typer_state_to_string(state: TyperState) -> str:
    return state.value if state is not None else "Unknown"
