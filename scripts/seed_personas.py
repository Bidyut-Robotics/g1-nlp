from services.memory.memory_manager import PersonasMemory

def seed():
    memory = PersonasMemory()
    
    # Seed a few personas
    personas = [
        {
            "id": "emp_001",
            "name": "Surya",
            "role": "Lead Researcher",
            "dept": "AI & Robotics",
            "pref": "Likes concise updates, drinks black coffee."
        },
        {
            "id": "emp_002",
            "name": "Ananya",
            "role": "Software Architect",
            "dept": "NLP",
            "pref": "Prefers detailed technical explanations."
        }
    ]
    
    for p in personas:
        person_id = p.pop("id")
        memory.upsert_persona(person_id, p)
        print(f"Seeded persona: {p['name']}")

if __name__ == "__main__":
    seed()
