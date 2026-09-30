"""Host-only PR identity locks; no container execution or HOME writes."""

from multiprocessing import get_context

from jailbee.outbox_io import ContainerIdentity


def _probe(identity, state, pipe):
    import os

    from jailbee.outbox.io import PrManagement

    os.environ["XDG_STATE_HOME"] = str(state)
    pipe.send("waiting")
    with PrManagement().lock(identity):
        pipe.send("acquired")
    pipe.close()


def test_pr_management_serializes_identity_and_reenters(tmp_path, monkeypatch):
    from jailbee.outbox.io import PrManagement

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    context = get_context("spawn")
    identity = ContainerIdentity("repo-feature", "2026-09-30T12:00:00Z")
    replacement = ContainerIdentity(identity.full_name, "2026-10-01T12:00:00Z")
    manager = PrManagement()
    processes = []
    pipes = []
    try:
        with manager.lock(identity):
            with manager.lock(identity):
                for candidate in (identity, replacement):
                    parent, child = context.Pipe()
                    process = context.Process(target=_probe, args=(candidate, tmp_path, child))
                    process.start()
                    child.close()
                    processes.append(process)
                    pipes.append(parent)
                    assert parent.poll(10), "worker did not reach the lock"
                    assert parent.recv() == "waiting"
                assert not pipes[0].poll(0.3), "same identity must remain blocked"
                assert pipes[1].poll(10), "replacement identity must not be blocked"
                assert pipes[1].recv() == "acquired"
            assert not pipes[0].poll(0.3), "inner release must not release the outer lock"
        assert pipes[0].poll(10)
        assert pipes[0].recv() == "acquired"
        for process in processes:
            process.join(10)
            assert process.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(10)
        for pipe in pipes:
            pipe.close()
