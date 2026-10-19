"""Example: a travel planning assistant."""

from telos import Agent, Goal

goal = Goal(
    name="italy trip",
    description=(
        "Help me plan a 2-week trip to Italy in September. "
        "I enjoy food, history, and off-the-beaten-path experiences. "
        "I'm traveling solo with a medium budget."
    ),
)

agent = Agent(goal)

turns = [
    "I'm thinking Rome, Florence, and maybe Sicily. Does that make sense for 2 weeks?",
    "What are some lesser-known spots near Florence?",
]

for user_msg in turns:
    print(f"\nYou: {user_msg}")
    print("Telos: ", end="", flush=True)
    for chunk in agent.stream(user_msg):
        print(chunk, end="", flush=True)
    print()
