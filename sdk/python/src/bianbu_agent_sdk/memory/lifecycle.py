"""SDK-owned Runtime result recording and lifecycle signal interpretation."""
from __future__ import annotations
import asyncio
import logging
logger = logging.getLogger(__name__)
async def record_runtime_result(bridge, run: dict) -> bool:
    if run['metadata'].get('console_operation') == 'generate_title':
        return False
    try:
        config = run.get('config', {})
        for compression in run['metadata'].get('context_compressions', [])[-8:]:
            try:
                from .types import SessionSummaryPatch
                from .runtime import _contains_credential
                summary = str(compression.get('summary') or '')[:8000]
                if not summary or _contains_credential(summary):
                    continue
                client = await bridge._client_or_none()
                if client is None:
                    break
                context = await client.sessions.get_context(run['session_id'])
                revision = int((context.get('summary') or {}).get('revision', 0))
                compression_id = str(compression.get('compression_id') or run['run_id'])
                await client.sessions.update_summary(run['session_id'], patch=SessionSummaryPatch(current_status=summary[:2000], context_pointers=(compression_id,)), source_event_sequence=int(context.get('last_event_sequence', 0)), expected_revision=revision, producer='runtime-context-compression', idempotency_key='runtime-compression:' + compression_id)
                await client.runtime.prepare_checkpoint(run['session_id'], idempotency_key='runtime-compression-checkpoint:' + compression_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning('AgentMemory compression persistence unavailable', exc_info=True)
        if run['status'] == 'completed' and run['response'].strip():
            return await asyncio.wait_for(
                bridge.record_turn(
                    run_id=run['run_id'], session_id=run['session_id'],
                    agent_id=run['agent_id'], user_text=run['request'],
                    assistant_text=run['response'],
                    force_summary=any(
                        bool(run['metadata'].get(key))
                        for key in ('session_end', 'conversation_end', 'close_session', 'finalize_session')
                    ),
                ),
                timeout=config.get('timeout_seconds', 2.0),
            )
        record_interrupted = getattr(bridge, 'record_interrupted_turn', None)
        if not callable(record_interrupted):
            return False
        return await asyncio.wait_for(
            record_interrupted(
                run_id=run['run_id'], session_id=run['session_id'],
                agent_id=run['agent_id'], user_text=run['request'],
                status=run['status'] or 'interrupted',
                error=str(run['metadata'].get('error') or '').strip() or None,
            ),
            timeout=config.get('timeout_seconds', 2.0),
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning('AgentMemory turn recording unavailable: %s', exc)
        return False


def record_legacy_result(data_root: str, run: dict) -> None:
    """Append to the explicitly selected legacy file backend."""
    from pathlib import Path
    import json
    import os
    content = str(run["metadata"].get("memory_to_record") or "").strip()
    if not content:
        return None
    Path(data_root).mkdir(parents=True, exist_ok=True)
    path = Path(data_root) / "memory.jsonl"
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"content": content}, ensure_ascii=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    return None

