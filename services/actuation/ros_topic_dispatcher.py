import json
import os
import shutil
import subprocess
from typing import Iterable

from core.schemas import NLPActionPayload


class ROSTopicDispatcher:
    """
    Publishes NLP action payloads to a ROS topic.
    Falls back to stdout when the ROS 2 CLI is unavailable.
    """

    def __init__(
        self,
        topic_name: str = "/humanoid/nlp/actions",
        message_type: str = "std_msgs/msg/String",
    ):
        self.topic_name = topic_name
        self.message_type = message_type
        self.ros2_cli = shutil.which(os.getenv("ROS2_CLI", "ros2"))

    def dispatch(self, action: NLPActionPayload) -> bool:
        payload = {
            "action_type": action.action_type.value,
            "params": action.params,
            "priority": action.priority,
            "utterance_id": action.utterance_id,
            "confidence": action.confidence,
        }
        serialized_payload = json.dumps(payload)

        if not self.ros2_cli:
            print(f"[ROS DISPATCH:FALLBACK] topic={self.topic_name} payload={serialized_payload}")
            return False

        command = [
            self.ros2_cli,
            "topic",
            "pub",
            "--once",
            self.topic_name,
            self.message_type,
            f"{{data: '{serialized_payload}'}}",
        ]

        try:
            subprocess.run(command, check=True, capture_output=True, text=True)
            print(f"[ROS DISPATCH] Published to {self.topic_name}: {serialized_payload}")
            return True
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr.strip() if exc.stderr else str(exc)
            print(f"[ROS DISPATCH:ERROR] {stderr}")
            return False

    def dispatch_many(self, actions: Iterable[NLPActionPayload]) -> int:
        published = 0
        for action in actions:
            if self.dispatch(action):
                published += 1
        return published
