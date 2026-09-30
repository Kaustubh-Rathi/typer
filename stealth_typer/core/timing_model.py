"""Port of StealthDesk ``src/core/timing_model.h``.

Two strategies for the delay between characters:

* ``FixedTimingModel``   - constant interval from the speed (humanization off)
* ``HumanLikeTimingModel`` - log-normal jitter plus contextual multipliers for
  punctuation, newlines and spaces.

The C++ uses ``std::mt19937`` seeded with 1337; Python's ``random.Random`` uses
the Mersenne Twister too, so the shape of the distribution is identical though
the exact number stream differs.
"""

import math
import random
from abc import ABC, abstractmethod
from enum import Enum

from .typing_policy import typing_interval_ms


class HumanizationLevel(Enum):
    """Port of ``stealthdesk::HumanizationLevel``."""

    OFF = "off"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ITypingTimingModel(ABC):
    """Port of ``stealthdesk::ITypingTimingModel``."""

    @abstractmethod
    def next_delay_ms(self, current_ch: int, next_ch: int, speed: int) -> int:
        ...

    @abstractmethod
    def reset(self) -> None:
        ...

    @abstractmethod
    def set_seed(self, seed: int) -> None:
        ...

    @abstractmethod
    def set_humanization_level(self, level: HumanizationLevel) -> None:
        ...

    @property
    @abstractmethod
    def humanization_level(self) -> HumanizationLevel:
        ...


class FixedTimingModel(ITypingTimingModel):
    def next_delay_ms(self, current_ch: int, next_ch: int, speed: int) -> int:
        return typing_interval_ms(speed)

    def reset(self) -> None:
        pass

    def set_seed(self, seed: int) -> None:
        pass

    def set_humanization_level(self, level: HumanizationLevel) -> None:
        pass

    @property
    def humanization_level(self) -> HumanizationLevel:
        return HumanizationLevel.OFF


class HumanLikeTimingModel(ITypingTimingModel):
    def __init__(
        self,
        level: HumanizationLevel = HumanizationLevel.MEDIUM,
        seed: int = 1337,
    ) -> None:
        self._level = level
        self._initial_seed = seed
        self._rng = random.Random(seed)

    def set_seed(self, seed: int) -> None:
        self._initial_seed = seed
        self._rng = random.Random(seed)

    def set_humanization_level(self, level: HumanizationLevel) -> None:
        self._level = level

    @property
    def humanization_level(self) -> HumanizationLevel:
        return self._level

    def reset(self) -> None:
        self._rng = random.Random(self._initial_seed)

    def next_delay_ms(self, current_ch: int, next_ch: int, speed: int) -> int:
        base = float(typing_interval_ms(speed))
        if self._level == HumanizationLevel.OFF:
            return int(base)

        sigma = 0.15
        punct_multiplier = 1.8
        newline_multiplier = 2.2
        space_multiplier = 1.3

        if self._level == HumanizationLevel.LOW:
            sigma = 0.08
            punct_multiplier = 1.3
            newline_multiplier = 1.6
            space_multiplier = 1.15
        elif self._level == HumanizationLevel.MEDIUM:
            sigma = 0.15
            punct_multiplier = 1.8
            newline_multiplier = 2.2
            space_multiplier = 1.3
        elif self._level == HumanizationLevel.HIGH:
            sigma = 0.25
            punct_multiplier = 2.5
            newline_multiplier = 3.0
            space_multiplier = 1.5

        # Log-normal distribution: X = exp(mu + sigma * Z), Z ~ N(0, 1).
        # Set median exp(mu) = base -> mu = ln(base).
        mu = math.log(max(1.0, base))
        z = self._rng.gauss(0.0, 1.0)
        delay = math.exp(mu + sigma * z)

        # Contextual modifiers.
        if current_ch in (ord("."), ord(","), ord("!"), ord("?"), ord(";"), ord(":")):
            delay *= punct_multiplier
        elif current_ch == ord("\n"):
            delay *= newline_multiplier
        elif current_ch == ord(" "):
            delay *= space_multiplier

        return int(max(5.0, min(2000.0, delay)))
