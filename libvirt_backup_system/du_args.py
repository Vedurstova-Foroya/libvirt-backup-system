from __future__ import annotations

from argparse import Namespace
from dataclasses import dataclass

from .logging_json import event
from .vms import is_safe_vm_uuid


@dataclass(frozen=True)
class DuFilters:
    host_id: str | None
    vm_uuid: str | None


def _merge_filter(name: str, current: str | None, proposed: str) -> str:
    if current is not None and current != proposed:
        event("error", f"conflicting du {name} filters", existing=current, positional=proposed)
        raise ValueError
    return proposed


def resolve_du_filters(args: Namespace) -> DuFilters | None:
    host_id = args.host_id
    vm_uuid = args.vm_uuid
    drilldown = list(args.drilldown or [])
    if len(drilldown) > 2:
        event("error", "du accepts at most two drilldown arguments")
        return None
    try:
        if len(drilldown) == 1:
            target = drilldown[0]
            if is_safe_vm_uuid(target):
                vm_uuid = _merge_filter("vm_uuid", vm_uuid, target)
            else:
                host_id = _merge_filter("host_id", host_id, target)
        elif len(drilldown) == 2:
            target_host, target_uuid = drilldown
            if is_safe_vm_uuid(target_host):
                event("error", "du host drilldown must precede VM UUID", value=target_host)
                return None
            if not is_safe_vm_uuid(target_uuid):
                event("error", "du second drilldown argument must be a VM UUID", value=target_uuid)
                return None
            host_id = _merge_filter("host_id", host_id, target_host)
            vm_uuid = _merge_filter("vm_uuid", vm_uuid, target_uuid)
    except ValueError:
        return None
    return DuFilters(host_id=host_id, vm_uuid=vm_uuid)
