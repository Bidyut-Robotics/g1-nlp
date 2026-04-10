from typing import List, Dict, Any

class VMSService:
    """
    Bridge to the Visitor Management System (VMS).
    Enables the robot to verify visitors and notify hosts.
    """
    def __init__(self):
        # Mock VMS data for Week 6 demo
        self.expected_visitors = [
            {"id": "v_001", "name": "John Doe", "purpose": "Interview", "host": "Surya", "status": "expected"},
            {"id": "v_002", "name": "Jane Smith", "purpose": "Client Meeting", "host": "Ananya", "status": "expected"}
        ]

    def lookup_visitor(self, name: str) -> Dict[str, Any]:
        """Checks if a visitor is expected today."""
        print(f"[VMS] Looking up visitor: {name}...")
        for v in self.expected_visitors:
            if name.lower() in v["name"].lower():
                return v
        return {"status": "not_found", "name": name}

    def register_arrival(self, visitor_id: str) -> bool:
        """Registers a visitor as arrived and simulates a host notification."""
        for v in self.expected_visitors:
            if v["id"] == visitor_id:
                v["status"] = "arrived"
                print(f"[VMS] SUCCESS: Host {v['host']} notified of {v['name']}'s arrival.")
                return True
        return False

    def get_visitor_details(self, visitor_id: str) -> Dict[str, Any]:
        """Retrieves full details for a visitor ID."""
        for v in self.expected_visitors:
            if v["id"] == visitor_id:
                return v
        return {}
