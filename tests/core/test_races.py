"""Race and lifecycle tests with barriers (no sleeps)."""
from __future__ import annotations

import threading


from sshpilot.core.connections import ConnectionService
from sshpilot.core.errors import CoreError
from sshpilot.core.import_export import MergeStrategy, apply_import_to_connection_dicts
from sshpilot.core.interaction import (
    InteractionOutcome,
    InteractionRequest,
    InteractionResponse,
    PromptKind,
    ResponseType,
    validate_response,
)
from sshpilot.core.transfers import (
    TransferDirection,
    TransferState,
    TransferSummary,
    transition,
)


def _run_five(fn):
    for _ in range(5):
        fn()


def test_connection_update_versus_delete_five_times(tmp_path):
    def once():
        svc = ConnectionService(store_path=tmp_path / "r.json", autosave=False)
        c = svc.create({"nickname": "R", "hostname": "r.test", "username": "u"})
        barrier = threading.Barrier(3)
        errors = []

        def upd():
            barrier.wait()
            try:
                svc.update(c.id, {"port": 2222})
            except CoreError:
                pass
            except Exception as exc:
                errors.append(exc)

        def dele():
            barrier.wait()
            try:
                svc.delete(c.id)
            except CoreError:
                pass
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=upd), threading.Thread(target=dele)]
        for t in threads:
            t.start()
        barrier.wait()
        for t in threads:
            t.join()
        assert not errors

    _run_five(once)


def test_import_versus_mutation_five_times():
    def once():
        existing = [{"nickname": "A", "hostname": "a", "username": "u"}]
        payload = {
            "version": 1,
            "connections": [{"nickname": "A", "hostname": "b", "username": "u"}],
        }
        barrier = threading.Barrier(3)
        errors = []

        def importer():
            barrier.wait()
            try:
                apply_import_to_connection_dicts(payload, existing, strategy=MergeStrategy.MERGE)
            except Exception as exc:
                errors.append(exc)

        def mutator():
            barrier.wait()
            try:
                existing.append({"nickname": "B", "hostname": "c", "username": "u"})
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=importer), threading.Thread(target=mutator)]
        for t in threads:
            t.start()
        barrier.wait()
        for t in threads:
            t.join()
        assert not errors

    _run_five(once)


def test_transfer_cancel_versus_complete_five_times():
    def once():
        summary = TransferSummary(
            id="t",
            direction=TransferDirection.UPLOAD,
            state=TransferState.RUNNING,
            source="a",
            destination="b",
        )
        barrier = threading.Barrier(3)
        results = []

        def cancel():
            barrier.wait()
            try:
                results.append(transition(summary, TransferState.CANCELLING))
            except CoreError as exc:
                results.append(exc)

        def complete():
            barrier.wait()
            try:
                results.append(transition(summary, TransferState.COMPLETED))
            except CoreError as exc:
                results.append(exc)

        threads = [threading.Thread(target=cancel), threading.Thread(target=complete)]
        for t in threads:
            t.start()
        barrier.wait()
        for t in threads:
            t.join()
        assert len(results) == 2

    _run_five(once)


def test_askpass_cancel_versus_submit_five_times():
    def once():
        req = InteractionRequest(kind=PromptKind.PASSWORD, attempt=1)
        barrier = threading.Barrier(3)
        outs = []

        def cancel():
            barrier.wait()
            outs.append(
                validate_response(
                    req,
                    InteractionResponse(
                        outcome=InteractionOutcome.CANCELLED,
                        response_type=ResponseType.CANCEL,
                    ),
                )
            )

        def submit():
            barrier.wait()
            outs.append(
                validate_response(
                    req,
                    InteractionResponse(
                        outcome=InteractionOutcome.ACCEPTED,
                        response_type=ResponseType.SECRET,
                        secret="x",
                    ),
                )
            )

        threads = [threading.Thread(target=cancel), threading.Thread(target=submit)]
        for t in threads:
            t.start()
        barrier.wait()
        for t in threads:
            t.join()
        assert len(outs) == 2

    _run_five(once)


def test_gtk_adapter_disposal_while_core_event_five_times():
    """Domain listeners must tolerate disposal mid-emit (no deadlock/exception)."""

    def once():
        svc = ConnectionService(autosave=False)
        dead = {"gone": False}
        barrier = threading.Barrier(2)

        def listener(_event):
            barrier.wait()
            if dead["gone"]:
                return
            dead["gone"] = True

        svc.add_listener(listener)

        def mutator():
            svc.create({"nickname": "Disp", "hostname": "d.test", "username": "u"})

        t = threading.Thread(target=mutator)
        t.start()
        barrier.wait()
        svc.remove_listener(listener)
        t.join()

    _run_five(once)


def test_plugin_unload_while_connection_mutation_five_times():
    """Simulated plugin unload (listener removal) during mutation."""

    def once():
        svc = ConnectionService(autosave=False)
        barrier = threading.Barrier(3)
        seen = []

        def plugin_listener(event):
            seen.append(event.kind)

        svc.add_listener(plugin_listener)

        def mutate():
            barrier.wait()
            svc.create({"nickname": "Plug", "hostname": "p.test", "username": "u"})

        def unload():
            barrier.wait()
            svc.remove_listener(plugin_listener)

        threads = [threading.Thread(target=mutate), threading.Thread(target=unload)]
        for t in threads:
            t.start()
        barrier.wait()
        for t in threads:
            t.join()

    _run_five(once)
