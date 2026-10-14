#!/bin/sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
cd "$repo_root"

fail() {
    printf 'FAIL: %s\n' "$1" >&2
    exit 1
}

source_name=$(awk '/^Source:/ { print $2; exit }' debian/control)
changelog_name=$(dpkg-parsechangelog -S Source)
[ "$source_name" = bianbu-agent-memory ] \
    || fail "unexpected Debian source package name: $source_name"
[ "$changelog_name" = "$source_name" ] \
    || fail "debian/control and debian/changelog source names differ"

binary_packages=$(awk '/^Package:/ { print $2 }' debian/control | sort)
expected_packages=$(printf '%s\n' bianbu-agent-memory bianbu-agent-memoryd | sort)
[ "$binary_packages" = "$expected_packages" ] \
    || fail "unexpected Debian binary package set"

grep -q '^ bianbu-agent-memoryd (= ${binary:Version}),$' debian/control \
    || fail "public Memory package must depend on the matching daemon version"
grep -q '^ bianbu-agent-memory-sdk (= ${binary:Version}),$' debian/control \
    || fail "public Memory package must provide the former SDK package name"
grep -q '^ bianbu-agent-memory-sdk (<< ${binary:Version}),$' debian/control \
    || fail "public Memory package must replace older SDK packages"

grep -q 'debian/bianbu-agent-memory/usr/lib/python3/dist-packages' debian/rules \
    || fail "SDK files are not staged in the public Memory package"
! grep -q 'debian/bianbu-agent-memory-sdk/' debian/rules \
    || fail "obsolete SDK package staging path remains"
grep -q '^name = "bianbu-agent-memory"$' sdk/python/pyproject.toml \
    || fail "Python distribution name does not match the public Memory package"

adapter_unit=packaging/bianbu-agent-memory-adapter.service
adapter_module=sdk/python/src/bianbu_agent_sdk/memory/bibit_adapter.py
adapter_dropin=packaging/bianbu-agent-memory-adapter.service.d/10-loopback.conf
grep -q -- '--host 127\.0\.0\.1' "$adapter_unit" \
    || fail "Memory adapter unit must bind the loopback address"
! grep -q '0\.0\.0\.0' "$adapter_unit" \
    || fail "Memory adapter unit must not bind every interface; tunnel instead"
grep -q 'add_argument("--host", default="127.0.0.1")' "$adapter_module" \
    || fail "Memory adapter must default to the loopback address"
! grep -q 'add_argument("--allow-origin", default="\*")' "$adapter_module" \
    || fail "Memory adapter must not default to a wildcard CORS origin"

# The drop-in is what keeps the adapter on loopback when a hand-copied unit in
# /etc/systemd/system shadows the packaged one, so it must exist, must reset
# ExecStart first, and must stay byte-identical to the unit's command line.
[ -f "$adapter_dropin" ] \
    || fail "Memory adapter loopback drop-in is not shipped"
grep -q '^ExecStart=$' "$adapter_dropin" \
    || fail "Memory adapter drop-in must reset ExecStart before overriding it"
! grep -q '0\.0\.0\.0' "$adapter_dropin" \
    || fail "Memory adapter drop-in must not bind every interface"
unit_execstart=$(grep -m1 '^ExecStart=' "$adapter_unit")
dropin_execstart=$(grep '^ExecStart=' "$adapter_dropin" | tail -n 1)
[ "$unit_execstart" = "$dropin_execstart" ] \
    || fail "Memory adapter unit and loopback drop-in disagree on ExecStart"
grep -q 'lib/systemd/system/bianbu-agent-memory-adapter.service.d/' debian/rules \
    || fail "the loopback drop-in is not staged by debian/rules"

# The D-Bus control plane is part of the M4 deliverable: the daemon must enable
# it, the policy must ship, and debian/rules must stage that policy, otherwise
# the interface silently disappears from the package.
dbus_policy=packaging/com.bianbu.AgentMemory1.conf
[ -f "$dbus_policy" ] \
    || fail "D-Bus policy for memoryd is not shipped"
grep -q 'allow own="com.bianbu.AgentMemory1"' "$dbus_policy" \
    || fail "D-Bus policy must let the daemon own com.bianbu.AgentMemory1"
# A client may only reach the service through a bus that lets it discover the
# interface at all; without these two standard interfaces the delivered
# control plane is unreachable from busctl/gdbus.
for iface in org.freedesktop.DBus.Introspectable org.freedesktop.DBus.Properties; do
    grep -q "send_interface=\"$iface\"" "$dbus_policy" \
        || fail "D-Bus policy does not let clients call $iface on memoryd"
done
grep -q '^ExecStart=.*--dbus' packaging/bianbu-agent-memoryd.service \
    || fail "memoryd unit must enable the D-Bus service"
grep -q 'usr/share/dbus-1/system.d/com.bianbu.AgentMemory1.conf' debian/rules \
    || fail "the D-Bus policy is not staged into the daemon package"

packaged_version=$(dpkg-parsechangelog -S Version)
reported_version=$(target/release/bianbu-agent-memoryd --version 2>/dev/null | awk '{ print $NF }')
if [ -n "$reported_version" ]; then
    [ "$reported_version" = "$packaged_version" ] \
        || fail "bianbu-agent-memoryd --version ($reported_version) does not match debian/changelog ($packaged_version)"
fi

printf 'Debian Memory package metadata is valid.\n'
