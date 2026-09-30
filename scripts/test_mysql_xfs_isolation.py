#!/usr/bin/env python3
"""
Test script for MySQL XFS reflink isolation.

This script validates that:
1. XFS reflink is working correctly
2. MySQL datadirs can be cloned quickly
3. MySQL cold branch lifecycle works without a per-agent container
4. The full isolation workflow functions end-to-end
"""
import argparse
import logging
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from omegaconf import OmegaConf


def run_command(cmd: list[str], check: bool = True, timeout: int = 30) -> tuple[int, str, str]:
    """Run a command and return (returncode, stdout, stderr)."""
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if check and result.returncode != 0:
        print(f"❌ Command failed: {' '.join(cmd)}")
        print(f"   stdout: {result.stdout}")
        print(f"   stderr: {result.stderr}")
        sys.exit(1)
    return result.returncode, result.stdout, result.stderr


def check_xfs_reflink(xfs_path: str) -> bool:
    """Check if the path is on XFS with reflink enabled."""
    print(f"\n📁 Checking filesystem type for {xfs_path}...")

    if not Path(xfs_path).exists():
        print(f"❌ Path does not exist: {xfs_path}")
        return False

    # Check filesystem type
    code, stdout, _ = run_command(
        ["stat", "-f", "-c", "%T", xfs_path],
        check=False,
    )
    fs_type = stdout.strip()
    print(f"   Filesystem type: {fs_type}")

    if fs_type != "xfs":
        print(f"⚠️  WARNING: Not on XFS (found {fs_type}), reflink may not work")
        return False

    # A direct clone probe is authoritative and also works for loop-mounted
    # XFS paths where xfs_info cannot discover the backing block device.
    with tempfile.NamedTemporaryFile(dir=xfs_path, delete=False) as source:
        source.write(b"web-agent-reflink-probe")
        source_path = Path(source.name)
    target_path = source_path.with_name(source_path.name + ".clone")
    try:
        code, _, _ = run_command(
            ["cp", "--reflink=always", str(source_path), str(target_path)],
            check=False,
        )
        if code == 0:
            print("   ✅ Reflink: enabled")
            return True
        print("   ❌ Reflink: NOT enabled")
        return False
    finally:
        source_path.unlink(missing_ok=True)
        target_path.unlink(missing_ok=True)


def test_reflink_clone(xfs_path: str, template_name: str) -> bool:
    """Test XFS reflink clone speed."""
    print(f"\n🚀 Testing reflink clone speed...")

    template_path = Path(xfs_path) / template_name
    test_clone_path = Path(xfs_path) / "test_reflink_clone"

    if not template_path.exists():
        print(f"❌ Template not found: {template_path}")
        return False

    # Get template size
    code, stdout, _ = run_command(["du", "-sh", str(template_path)])
    template_size = stdout.split()[0]
    print(f"   Template size: {template_size}")

    # Clean up if exists
    if test_clone_path.exists():
        run_command(["rm", "-rf", str(test_clone_path)], check=False)

    # Test reflink clone
    start = time.time()
    code, stdout, stderr = run_command(
        ["cp", "--reflink=always", "-r", str(template_path), str(test_clone_path)],
        check=False,
    )
    elapsed = time.time() - start

    if code != 0:
        print(f"❌ Reflink clone failed: {stderr}")
        return False

    print(f"   ✅ Clone time: {elapsed:.2f}s")

    if elapsed > 2.0:
        print(f"⚠️  WARNING: Clone took > 2s, reflink may not be working properly")

    # Check clone size
    code, stdout, _ = run_command(["du", "-sh", str(test_clone_path)])
    clone_size = stdout.split()[0]
    print(f"   Clone size: {clone_size}")

    # Check disk usage (should be minimal with CoW)
    code, stdout, _ = run_command(["df", "-h", xfs_path])
    print(f"   Disk usage after clone:")
    for line in stdout.strip().split("\n")[1:]:
        print(f"      {line}")

    # Clean up
    run_command(["rm", "-rf", str(test_clone_path)], check=False)

    return elapsed < 2.0


def test_mysql_branch_lifecycle(
    xfs_path: str, template_name: str, runtime_root: str
) -> bool:
    """Test the CoW branch lifecycle without creating a Docker container."""
    print("\n🐬 Testing MySQL branch lifecycle...")
    from rl_web_agent.isolation.mysql_xfs_isolation import MySQLXFSReflinkManager

    manager = MySQLXFSReflinkManager(
        OmegaConf.create(
            {
                "xfs_base_path": xfs_path,
                "runtime_root": runtime_root,
                "template_name": template_name,
                "launch_enabled": False,
                "mysql_port_base": 13999,
            }
        ),
        logging.getLogger("mysql-lifecycle-test"),
    )
    resource = manager.prepare("test_environment")
    checkpoint = manager.checkpoint(resource, "checkpoint_test")
    child = manager.fork(resource, "checkpoint_test", "left")
    reset = manager.reset(child)
    manager.cleanup(reset)
    print(
        "   ✅ prepare/checkpoint/fork/reset/cleanup completed "
        f"(checkpoint={checkpoint.datadir}, child={child.datadir})"
    )
    return True


def test_python_integration() -> bool:
    """Test that Python can import the MySQL isolation module."""
    print(f"\n🐍 Testing Python integration...")

    try:
        from rl_web_agent.isolation.mysql_xfs_isolation import MySQLXFSReflinkManager
        print("   ✅ MySQLXFSReflinkManager imported successfully")
        return True
    except ImportError as e:
        print(f"   ❌ Failed to import: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Test MySQL XFS reflink isolation")
    parser.add_argument(
        "--xfs-path",
        default="/var/lib/web-agent/pg18_clone_xfs/mysql_clone_xfs",
        help="Path to XFS partition",
    )
    parser.add_argument(
        "--template-name",
        default="magento_template",
        help="MySQL template datadir name",
    )
    parser.add_argument(
        "--skip-branch",
        action="store_true",
        help="Skip the reflink branch lifecycle test",
    )
    parser.add_argument(
        "--runtime-root",
        default="/var/lib/web-agent/pg18_clone_xfs/mysql_clone_xfs/runtime-test",
        help="Runtime root for branch/checkpoint trees",
    )
    args = parser.parse_args()

    print("=" * 60)
    print("MySQL XFS Reflink Isolation Test")
    print("=" * 60)

    results = {}

    # Test 1: Check XFS reflink
    results["xfs_reflink"] = check_xfs_reflink(args.xfs_path)

    # Test 2: Clone speed
    if results["xfs_reflink"]:
        results["clone_speed"] = test_reflink_clone(args.xfs_path, args.template_name)
    else:
        print("\n⚠️  Skipping clone test (XFS reflink not available)")
        results["clone_speed"] = False

    # Test 3: branch lifecycle; this must not launch a per-agent container.
    if not args.skip_branch and results["clone_speed"]:
        results["mysql_branch_lifecycle"] = test_mysql_branch_lifecycle(
            args.xfs_path, args.template_name, args.runtime_root
        )
    else:
        if args.skip_branch:
            print("\n⚠️  Skipping branch lifecycle test (--skip-branch)")
        results["mysql_branch_lifecycle"] = None

    # Test 4: Python integration
    results["python_integration"] = test_python_integration()

    # Summary
    print("\n" + "=" * 60)
    print("Test Summary")
    print("=" * 60)

    for test_name, result in results.items():
        if result is None:
            status = "⊘ SKIPPED"
        elif result:
            status = "✅ PASS"
        else:
            status = "❌ FAIL"
        print(f"{status}  {test_name}")

    # Overall result
    print("\n" + "=" * 60)

    failed = [name for name, result in results.items() if result is False]
    if failed:
        print(f"❌ FAILED: {', '.join(failed)}")
        print("\nTo fix:")
        if "xfs_reflink" in failed:
            print("  1. Ensure /var/lib/web-agent is on XFS with reflink=1")
            print("  2. Check: xfs_info /var/lib/web-agent | grep reflink")
        if "clone_speed" in failed:
            print("  3. Verify reflink is working: cp --reflink=always test1 test2")
        if "mysql_branch_lifecycle" in failed:
            print("  4. Check the XFS template and runtime permissions")
        if "python_integration" in failed:
            print("  5. Ensure you're in the CUA-Sandbox project directory")
        sys.exit(1)
    else:
        print("✅ ALL TESTS PASSED")
        print("\nYou can now enable MySQL XFS reflink isolation:")
        print("  environment.db_isolation.mysql_xfs.enabled=true")
        sys.exit(0)


if __name__ == "__main__":
    main()
