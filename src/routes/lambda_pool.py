"""Lambda Pool routes — dynamic scaling, invocation, status."""

import logging

from fastapi import Request
from fastapi.responses import JSONResponse

from ..lambda_pool import LambdaPool

log = logging.getLogger("acp-bridge.routes.lambda_pool")


def register(app, lambda_pool: LambdaPool | None):
    """Register /lambda-pool/* endpoints."""

    if not lambda_pool:
        # Stub endpoints that return 503 when lambda_pool is not configured
        @app.get("/lambda-pool/status")
        async def status_disabled():
            return JSONResponse({"error": "lambda_pool not enabled"}, status_code=503)

        @app.post("/lambda-pool/scale")
        async def scale_disabled(request: Request):
            return JSONResponse({"error": "lambda_pool not enabled"}, status_code=503)

        @app.post("/lambda-pool/invoke")
        async def invoke_disabled(request: Request):
            return JSONResponse({"error": "lambda_pool not enabled"}, status_code=503)

        @app.post("/lambda-pool/drain")
        async def drain_disabled():
            return JSONResponse({"error": "lambda_pool not enabled"}, status_code=503)

        return

    @app.get("/lambda-pool/status")
    async def lambda_pool_status():
        """Get Lambda pool status and stats."""
        return lambda_pool.stats

    @app.post("/lambda-pool/scale")
    async def lambda_pool_scale(request: Request):
        """Pre-warm Lambda containers.

        Body: {"count": 100, "profile": "developer"}
        """
        body = await request.json()
        count = body.get("count", 10)
        count = min(count, lambda_pool._max_concurrent)

        result = await lambda_pool.warmup(count=count)
        log.info("lambda_pool scale: requested=%d result=%s", count, result)
        return result

    @app.post("/lambda-pool/invoke")
    async def lambda_pool_invoke(request: Request):
        """Invoke a single Lambda harness agent.

        Body: {
          "prompt": "...",
          "profile": {...},   // optional
          "model": "...",     // optional
          "timeout": 300      // optional
        }
        """
        body = await request.json()
        prompt = body.get("prompt", "")
        if not prompt:
            return JSONResponse({"error": "prompt is required"}, status_code=400)

        profile = body.get("profile")
        model = body.get("model", "")
        timeout = body.get("timeout", 0)

        result = await lambda_pool.invoke(
            prompt=prompt,
            profile=profile,
            model=model,
            timeout=timeout,
        )
        status_code = 200 if result.get("status") == "completed" else 502
        return JSONResponse(result, status_code=status_code)

    @app.post("/lambda-pool/invoke-batch")
    async def lambda_pool_invoke_batch(request: Request):
        """Invoke multiple Lambda agents in parallel.

        Body: {
          "prompts": [
            {"prompt": "task 1", "session_id": "optional-1"},
            {"prompt": "task 2"},
            ...
          ],
          "profile": {...},   // shared profile for all
          "model": "..."      // shared model for all
        }
        """
        body = await request.json()
        prompts = body.get("prompts", [])
        if not prompts:
            return JSONResponse({"error": "prompts array is required"}, status_code=400)

        profile = body.get("profile")
        model = body.get("model", "")

        results = await lambda_pool.invoke_batch(
            prompts=prompts,
            profile=profile,
            model=model,
        )
        completed = sum(1 for r in results if r.get("status") == "completed")
        return {
            "total": len(results),
            "completed": completed,
            "failed": len(results) - completed,
            "results": results,
        }

    @app.post("/lambda-pool/drain")
    async def lambda_pool_drain():
        """Wait for all active Lambda invocations to complete."""
        result = await lambda_pool.drain()
        return result
