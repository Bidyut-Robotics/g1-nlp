import json
import asyncio
from typing import List, Dict, Any

class CalendarMCPTool:
    """
    Model Context Protocol (MCP) compatible Calendar Tool.
    Exposes employee schedules to the LLM.
    """
    def __init__(self):
        # Mock database of schedules for Week 4 demo
        self.mock_schedules = {
            "emp_001": [
                {"time": "10:00 AM", "event": "Standup Meeting", "room": "402"},
                {"time": "02:00 PM", "event": "Project Alpha Sync", "room": "Conference B"}
            ],
            "emp_002": [
                {"time": "11:30 AM", "event": "Interview: Frontend Lead", "room": "HR Room 1"}
            ]
        }

    async def get_schedule(self, employee_name: str) -> str:
        """
        Tool called by LLM to check an employee's schedule.
        Returns a formatted string of meetings.
        """
        # In a real MCP server, this would be a @mcp.tool()
        # For now, we simulate the retrieval logic
        print(f"[MCP] Fetching schedule for: {employee_name}...")
        
        # Simple name-to-id mapping simulation
        emp_id = "emp_001" if "surya" in employee_name.lower() else "emp_002"
        
        schedule = self.mock_schedules.get(emp_id, [])
        if not schedule:
            return f"No scheduled meetings found for {employee_name} today."
            
        res = f"Today's schedule for {employee_name}:\n"
        for item in schedule:
            res += f"- {item['time']}: {item['event']} (Room: {item['room']})\n"
            
        return res

    def get_tool_metadata(self) -> Dict[str, Any]:
        """Returns the MCP tool definition for the LLM's function list."""
        return {
            "name": "get_calendar_schedule",
            "description": "Fetch the daily meeting schedule for a specific employee.",
            "parameters": {
                "type": "object",
                "properties": {
                    "employee_name": {"type": "string", "description": "The name of the employee."}
                },
                "required": ["employee_name"]
            }
        }
