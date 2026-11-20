from Bplustree import BPlusTree


#Create a B+ Tree of order 3
tree = BPlusTree(order=3)


#TEST 1: Insert keys
print("\n===== TEST 1: INSERTION =====")

keys = [5, 6, 7, 30, 40, 60, 1, 10, 20, 50]

for key in keys:
    tree.insert(key)

tree.display()


#TEST 2: Search
print("\n===== TEST 2: SEARCH =====")

print("Search 30:", tree.search(30))
print("Search 100:", tree.search(100))
print("Search 1:", tree.search(1))


#TEST 3: Duplicate insertion
print("\n===== TEST 3: DUPLICATES =====")

result = tree.insert(30)

print("Duplicate insertion result:", result)


#TEST 4: Range scan
print("\n===== TEST 4: RANGE SCAN =====")

print("Range 5 to 30:", tree.range_scan(5, 30))
print("Range 1 to 10:", tree.range_scan(1, 10))
print("Range 35 to 60:", tree.range_scan(35, 60))


#TEST 5: Leaf chain
print("\n===== TEST 5: LEAF CHAIN =====")

tree.display_leaves()


#TEST 6: Verify all keys
print("\n===== TEST 6: VERIFY KEYS =====")

expected = sorted(set(keys))

actual = tree.range_scan(-1000, 1000)

print("Expected:", expected)
print("Actual:  ", actual)

assert actual == expected

print("All keys are correct!")
