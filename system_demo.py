from pathlib import Path
from tempfile import TemporaryDirectory

from coredb import Column, Config, Database
from coredb.storage import AuthenticatedStreamHook, ObjectStore


def main() -> None:
    with TemporaryDirectory(prefix="coredb_demo_") as temp_dir:
        temp = Path(temp_dir)
        db_path = temp / "retail.db"

        print("=== M1: Create an NSM table ===")
        with Database(
            str(db_path),
            Config(buffer_frames=16, replacement_policy="lru", default_layout="nsm"),
        ) as db:
            products = db.create_table(
                "products",
                [
                    Column("id", "INTEGER", nullable=False),
                    Column("name", "VARCHAR", max_length=100),
                    Column("price", "DECIMAL", precision=10, scale=2),
                ],
                layout="nsm",
            )

            products.insert((1, "Laptop", "1200.00"))
            products.insert((2, "Mouse", "25.50"))
            products.insert((3, "Keyboard", "75.00"))

            print("Tables:", db.tables())
            print("Rows:", [row for _, row in products.scan()])
            print("Database statistics:", db.stats())

            print("\n=== M2: Create a B+ Tree index ===")
            db.create_index(
                "products",
                "id",
                unique=True,
                index_type="btree",
            )

            result = db.execute(
                "SELECT id, name, price FROM products WHERE id = 2"
            )
            print("Query plan:", result.plan)
            print("Index type:", result.index_type)
            print("Query rows:", result.rows)

            print("\n=== M2: Indexed range query ===")
            range_result = db.execute(
                "SELECT id, name, price "
                "FROM products "
                "WHERE id BETWEEN 1 AND 2 "
                "ORDER BY id ASC"
            )
            print("Query plan:", range_result.plan)
            print("Range rows:", range_result.rows)

            print("\n=== M2: Encrypted object storage ===")
            store = ObjectStore(
                temp / "objects",
                encryption_hook=AuthenticatedStreamHook(
                    b"demo-retail-encryption-key"
                ),
            )
            original = b"\x89PNG\r\nDemo product image"
            metadata = store.put(original, content_type="image/png")

            print("Object URI:", metadata.uri)
            print("Encrypted at rest:", metadata.encrypted)
            print("Restored bytes:", store.get(metadata.uri))
            print("Checksum verified:", store.get(metadata.uri) == original)

            print("\n=== Final M1/M2 statistics ===")
            print(db.stats())


if __name__ == "__main__":
    main()
