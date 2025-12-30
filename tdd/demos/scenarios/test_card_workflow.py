"""
Demo: Complete Card Workflow

This scenario demonstrates the full lifecycle of a card from creation
through to completion. It serves as both a smoke test and executable
documentation of the intended user workflow.

Run with: pytest tdd/demos -v -m demo
"""
import sys
from pathlib import Path

import pytest

# Add backend and tdd to path for imports
backend_path = Path(__file__).parent.parent.parent.parent / "backend"
tdd_path = Path(__file__).parent.parent.parent.parent / "tdd"
sys.path.insert(0, str(backend_path))
sys.path.insert(0, str(tdd_path))


@pytest.mark.demo
class TestCardWorkflowDemo:
    """
    Demonstrates the complete card workflow:

    1. Create a repository
    2. Create a feature card
    3. Start work on the card (triggers agent)
    4. Card moves through statuses
    5. Approve the completed work
    6. Card reaches DONE status

    This test documents the happy path for feature development.
    """

    async def test_complete_card_lifecycle(self, client, clean_git_repos, clean_job_queue):
        """
        SCENARIO: Feature Development Lifecycle

        GIVEN a repository is attached to LazyAF
        WHEN a user creates a feature card
        AND starts work on the card
        AND approves the resulting PR
        THEN the card should reach DONE status
        """
        # Step 1: Attach a repository (must be ingested to start cards)
        print("\n=== Step 1: Attaching Repository ===")
        repo_response = await client.post(
            "/api/repos/ingest",
            json={
                "name": "demo-project",
                "remote_url": "https://github.com/org/demo-project.git",
                "default_branch": "main",
            },
        )
        assert repo_response.status_code == 201
        repo = repo_response.json()
        print(f"Created repo: {repo['name']} (ID: {repo['id'][:8]}...)")

        # Step 2: Create a feature card
        print("\n=== Step 2: Creating Feature Card ===")
        card_response = await client.post(
            f"/api/repos/{repo['id']}/cards",
            json={
                "title": "Add user authentication",
                "description": """
                    Implement user authentication with the following requirements:
                    - Email/password login
                    - Password hashing with bcrypt
                    - JWT token generation
                    - Protected route middleware
                """,
            },
        )
        assert card_response.status_code == 201
        card = card_response.json()
        print(f"Created card: {card['title']}")
        print(f"Initial status: {card['status']}")
        assert card["status"] == "todo"

        # Step 3: Start work on the card
        print("\n=== Step 3: Starting Work ===")
        start_response = await client.post(f"/api/cards/{card['id']}/start")
        assert start_response.status_code == 200
        card = start_response.json()
        print(f"Card status after start: {card['status']}")
        assert card["status"] == "in_progress"

        # Step 4: Simulate work completion (in real scenario, agent does this)
        print("\n=== Step 4: Simulating Work Completion ===")
        # Move to in_review (simulating agent completing work and creating PR)
        update_response = await client.patch(
            f"/api/cards/{card['id']}",
            json={"status": "in_review"},
        )
        assert update_response.status_code == 200
        card = update_response.json()
        print(f"Card status after work: {card['status']}")
        assert card["status"] == "in_review"

        # Step 5: Approve the work
        print("\n=== Step 5: Approving Work ===")
        approve_response = await client.post(
            f"/api/cards/{card['id']}/approve",
            json={"target_branch": None},
        )
        assert approve_response.status_code == 200
        result = approve_response.json()
        card = result["card"]
        print(f"Final card status: {card['status']}")
        assert card["status"] == "done"
        if result.get("merge_result"):
            print(f"Merge result: {result['merge_result']['message']}")

        print("\n=== Workflow Complete ===")
        print(f"Card '{card['title']}' successfully completed!")

    async def test_card_rejection_workflow(self, client):
        """
        SCENARIO: Feature Rejection and Retry

        GIVEN a card is in review
        WHEN the reviewer rejects the work
        THEN the card should return to TODO status
        AND the branch/PR information should be cleared
        """
        print("\n=== Card Rejection Workflow ===")

        # Setup: Create repo and card
        repo_response = await client.post(
            "/api/repos",
            json={"name": "reject-demo"},
        )
        repo = repo_response.json()

        card_response = await client.post(
            f"/api/repos/{repo['id']}/cards",
            json={"title": "Feature to reject"},
        )
        card = card_response.json()

        # Move card to in_review
        await client.patch(
            f"/api/cards/{card['id']}",
            json={"status": "in_review"},
        )
        print(f"Card in review: {card['title']}")

        # Reject the card
        reject_response = await client.post(f"/api/cards/{card['id']}/reject")
        assert reject_response.status_code == 200
        card = reject_response.json()

        print(f"Card status after rejection: {card['status']}")
        assert card["status"] == "todo"
        assert card["branch_name"] is None
        assert card["pr_url"] is None

        print("Card successfully rejected and reset for retry!")


@pytest.mark.demo
class TestMultiCardBoardDemo:
    """
    Demonstrates managing multiple cards on a Kanban board.
    """

    async def test_kanban_board_state(self, client):
        """
        SCENARIO: Kanban Board with Cards in Multiple States

        GIVEN a repository with multiple feature cards
        WHEN cards are in different workflow stages
        THEN the board should correctly reflect all card states
        """
        print("\n=== Multi-Card Board Demo ===")

        # Create repository
        repo_response = await client.post(
            "/api/repos",
            json={"name": "kanban-demo"},
        )
        repo = repo_response.json()
        print(f"Repository: {repo['name']}")

        # Create cards in different states
        cards_data = [
            {"title": "Feature A - Planning", "target_status": "todo"},
            {"title": "Feature B - In Development", "target_status": "in_progress"},
            {"title": "Feature C - Under Review", "target_status": "in_review"},
            {"title": "Feature D - Completed", "target_status": "done"},
        ]

        created_cards = []
        for card_data in cards_data:
            response = await client.post(
                f"/api/repos/{repo['id']}/cards",
                json={"title": card_data["title"], "description": "Demo card"},
            )
            card = response.json()

            # Transition to target status
            if card_data["target_status"] != "todo":
                await client.patch(
                    f"/api/cards/{card['id']}",
                    json={"status": card_data["target_status"]},
                )
                card["status"] = card_data["target_status"]

            created_cards.append(card)
            print(f"  [{card_data['target_status'].upper():12}] {card_data['title']}")

        # Verify board state
        list_response = await client.get(f"/api/repos/{repo['id']}/cards")
        cards = list_response.json()
        assert len(cards) == 4

        status_counts = {}
        for card in cards:
            status = card["status"]
            status_counts[status] = status_counts.get(status, 0) + 1

        print("\n=== Board Summary ===")
        for status, count in sorted(status_counts.items()):
            print(f"  {status}: {count} card(s)")

        assert status_counts.get("todo", 0) == 1
        assert status_counts.get("in_progress", 0) == 1
        assert status_counts.get("in_review", 0) == 1
        assert status_counts.get("done", 0) == 1
