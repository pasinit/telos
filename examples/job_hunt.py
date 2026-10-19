"""Example: a job-hunting assistant."""

from telos import Agent, Goal

goal = Goal(
    name="job hunt",
    description=(
        "Help me find and land a senior software engineering role. "
        "Track applications, prep for interviews, review resumes, and "
        "suggest companies that match my interests."
    ),
)

agent = Agent(goal)

# Simulate a multi-turn session
turns = [
    "I have an interview at Stripe next Tuesday for a senior backend role.",
    "What should I focus on to prepare for their system design round?",
    "Can you give me a mock question?",
]

for user_msg in turns:
    print(f"\nYou: {user_msg}")
    print("Telos: ", end="", flush=True)
    for chunk in agent.stream(user_msg):
        print(chunk, end="", flush=True)
    print()
