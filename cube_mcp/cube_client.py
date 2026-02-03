"""HTTP client for Cube.dev REST API."""
import os
from typing import Any

import httpx


class CubeClient:
    """Async HTTP client for Cube.dev REST API.

    Environment variables:
        CUBE_API_URL: Base URL for Cube REST API (required)
        CUBE_API_TOKEN: JWT token for authentication (optional in dev mode)
    """

    def __init__(self):
        self.base_url = os.environ.get("CUBE_API_URL", "http://localhost:4000/cubejs-api/v1")
        self.token = os.environ.get("CUBE_API_TOKEN")

    def _headers(self) -> dict[str, str]:
        """Build request headers with optional auth token."""
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = self.token
        return headers

    async def get_meta(self) -> dict[str, Any]:
        """Get metadata about all cubes and views.

        Returns:
            Dict with 'cubes' key containing list of cube metadata
        """
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self.base_url}/meta",
                headers=self._headers(),
                timeout=30.0
            )
            response.raise_for_status()
            return response.json()

    async def load(self, query: dict[str, Any]) -> dict[str, Any]:
        """Execute a Cube query and return results.

        Args:
            query: Cube query object with measures, dimensions, filters, etc.

        Returns:
            Dict with 'data' key containing query results
        """
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                f"{self.base_url}/load",
                json={"query": query},
                headers=self._headers()
            )
            response.raise_for_status()
            return response.json()

    async def get_sql(self, query: dict[str, Any]) -> dict[str, Any]:
        """Get the SQL that would be generated for a Cube query.

        Args:
            query: Cube query object with measures, dimensions, filters, etc.

        Returns:
            Dict with 'sql' key containing the generated SQL
        """
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.base_url}/sql",
                json={"query": query},
                headers=self._headers(),
                timeout=30.0
            )
            response.raise_for_status()
            return response.json()


# Global client instance
_cube_client: CubeClient | None = None


def get_cube_client() -> CubeClient:
    """Get or create the global CubeClient instance."""
    global _cube_client
    if _cube_client is None:
        _cube_client = CubeClient()
    return _cube_client
