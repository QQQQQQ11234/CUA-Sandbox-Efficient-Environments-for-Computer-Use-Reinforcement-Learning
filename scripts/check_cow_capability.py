#!/usr/bin/env python3
"""
Check if PostgreSQL CoW clone is actually working with reflink.
"""
import subprocess
import time
import sys

def check_filesystem_type(path="/var/lib/postgresql"):
    """Check filesystem type."""
    try:
        result = subprocess.run(
            ["df", "-T", path],
            capture_output=True,
            text=True,
            timeout=5
        )
        lines = result.stdout.strip().split('\n')
        if len(lines) > 1:
            fs_type = lines[1].split()[1]
            return fs_type
    except Exception as e:
        return f"Error: {e}"

def check_pg_version():
    """Check PostgreSQL version."""
    try:
        # Try docker exec if GitLab container exists
        result = subprocess.run(
            ["docker", "exec", "gitlab", "gitlab-psql", "-d", "postgres", "-c", "SELECT version();"],
            capture_output=True,
            text=True,
            timeout=10
        )
        return result.stdout
    except Exception as e:
        return f"Error: {e}"

def test_clone_speed():
    """Test actual clone speed to determine if CoW is working."""
    print("\n" + "="*60)
    print("PostgreSQL Clone Speed Test")
    print("="*60)

    # This would require actual DB connection
    # For now, just show the diagnostic info
    print("\n⚠️  To test clone speed, run this SQL in your PostgreSQL:")
    print("""
    -- Create a test database
    CREATE DATABASE test_source;

    -- Add some data
    \\c test_source
    CREATE TABLE test_data AS
    SELECT generate_series(1, 1000000) as id, md5(random()::text) as data;

    -- Time the clone
    \\timing on
    CREATE DATABASE test_clone TEMPLATE test_source STRATEGY FILE_COPY;

    -- Clean up
    DROP DATABASE test_clone;
    DROP DATABASE test_source;
    """)

    print("\n📊 Expected results:")
    print("  - With reflink CoW (XFS/Btrfs/ZFS): < 1 second")
    print("  - Without reflink (ext4): 10-60 seconds")

def main():
    print("="*60)
    print("PostgreSQL CoW Capability Check")
    print("="*60)

    # Check filesystem
    print("\n1. Filesystem Type:")
    fs_type = check_filesystem_type("/var/lib/web-agent")
    print(f"   Current: {fs_type}")

    if fs_type in ["xfs", "btrfs", "zfs"]:
        print("   ✅ Supports reflink CoW")
    elif fs_type == "ext4":
        print("   ❌ Does NOT support reflink CoW")
        print("   ⚠️  FILE_COPY will fall back to byte-by-byte copy!")
    else:
        print(f"   ⚠️  Unknown filesystem: {fs_type}")

    # Check PostgreSQL version
    print("\n2. PostgreSQL Version:")
    pg_version = check_pg_version()
    print(f"   {pg_version}")

    if "PostgreSQL 18" in pg_version or "PostgreSQL 19" in pg_version:
        print("   ✅ Supports FILE_COPY strategy")
    else:
        print("   ⚠️  FILE_COPY requires PostgreSQL 18+")

    # Show test instructions
    test_clone_speed()

    # Summary
    print("\n" + "="*60)
    print("SUMMARY")
    print("="*60)

    if fs_type == "ext4":
        print("\n❌ Your current setup CANNOT use true CoW cloning because:")
        print("   - Filesystem: ext4 (no reflink support)")
        print("   - PostgreSQL FILE_COPY will fall back to slow copy")
        print("\n💡 To enable true CoW cloning (200ms for 6GB):")
        print("   1. Migrate PostgreSQL data directory to XFS/Btrfs/ZFS")
        print("   2. Or use ZFS/Btrfs snapshots at filesystem level")
        print("   3. Keep ext4 and accept slower clones (30-60s)")
    else:
        print(f"\n✅ Your filesystem ({fs_type}) supports CoW")
        print("   Run the SQL test above to verify actual clone speed")

if __name__ == "__main__":
    main()
