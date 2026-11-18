"""Read-only evidence lookup over the engine's public query API (SPEC 6, ENG-12).

Only DecisionEngine.get_observation() and DecisionEngine.is_fresh() are used;
the engine window and counters are never read or changed from here.
"""

from voa.contracts.observation import Observation
from voa.engine.engine import DecisionEngine


class ObservationRepository:
    def __init__(self, engine: DecisionEngine) -> None:
        self._engine = engine

    def get(self, observation_id: str) -> Observation | None:
        return self._engine.get_observation(observation_id)

    def is_fresh(self, observation_id: str, now_mono_ns: int) -> bool:
        """Raises ValueError if now is earlier than the engine's last time."""
        return self._engine.is_fresh(observation_id, now_mono_ns)

    def session_of(self, observation_id: str) -> str | None:
        observation = self.get(observation_id)
        return observation.session_id if observation is not None else None
