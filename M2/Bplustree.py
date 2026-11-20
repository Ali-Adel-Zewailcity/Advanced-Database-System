class BPlusTreeNode:
    def __init__(self, leaf=False):

        self.leaf = leaf

        self.keys = []

        self.children = []

        self.next = None

        self.parent = None



class BPlusTree:
    def __init__(self, order=3):
        self.order = order
        self.root = BPlusTreeNode(leaf=True)
    
    def find_leaf(self, key):
        current = self.root

        while not current.leaf:

            i = 0

            while i < len(current.keys) and key >= current.keys[i]:
                i += 1

            current = current.children[i]

        return current

    def search(self, key):

        leaf = self.find_leaf(key)

        if key in leaf.keys:
            return True

        return False

    def range_scan(self, start, end):

        result = []

        current = self.find_leaf(start)

        while current:

            for key in current.keys:

                if start <= key <= end:
                    result.append(key)

                elif key > end:
                    return result

            current = current.next

        return result

    def insert_simple(self, key):


        leaf = self.find_leaf(key)


        leaf.keys.append(key)


        leaf.keys.sort()
    
    def insert(self, key):

        if self.search(key):
            print(f"Key {key} already exists.")
            return

        leaf = self.find_leaf(key)

        leaf.keys.append(key)
        leaf.keys.sort()

        if len(leaf.keys) > self.order:
            self.split_leaf(leaf)
    
    def split_leaf(self, leaf):

        new_leaf = BPlusTreeNode(leaf=True)

        mid = (self.order + 1) // 2

        new_leaf.keys = leaf.keys[mid:]
        leaf.keys = leaf.keys[:mid]

        new_leaf.next = leaf.next
        leaf.next = new_leaf

        separator = new_leaf.keys[0]
        
        if leaf == self.root:

            new_root = BPlusTreeNode(leaf=False)

            new_root.keys = [separator]

            new_root.children = [leaf, new_leaf]

            leaf.parent = new_root
            new_leaf.parent = new_root

            self.root = new_root

        else:

            parent = leaf.parent

            self.insert_into_parent(parent, separator, new_leaf)

    def insert_into_parent(self, parent, key, new_child):

        insert_index = 0

        while insert_index < len(parent.keys) and key > parent.keys[insert_index]:
            insert_index += 1

        parent.keys.insert(insert_index, key)

        parent.children.insert(insert_index + 1, new_child)

        new_child.parent = parent

        if len(parent.keys) > self.order:
            self.split_internal(parent)

    def split_internal(self, node):

        new_internal = BPlusTreeNode(leaf=False)

        mid = len(node.keys) // 2

        promoted_key = node.keys[mid]

        new_internal.keys = node.keys[mid + 1:]
        node.keys = node.keys[:mid]

        new_internal.children = node.children[mid + 1:]
        node.children = node.children[:mid + 1]

        for child in new_internal.children:
            child.parent = new_internal

        if node == self.root:

            new_root = BPlusTreeNode(leaf=False)

            new_root.keys = [promoted_key]
            new_root.children = [node, new_internal]

            node.parent = new_root
            new_internal.parent = new_root

            self.root = new_root

        else:

            parent = node.parent

            self.insert_into_parent(parent, promoted_key, new_internal)


    def display(self, node=None, level=0):

        if node is None:
            node = self.root

        indent = "    " * level

        node_type = "Leaf" if node.leaf else "Internal"

        print(f"{indent}{node_type} Node: {node.keys}")

        if not node.leaf:
            for child in node.children:
                self.display(child, level + 1)


    def display_leaves(self):

        print("\nLeaf Chain:")

        current = self.root

        while not current.leaf:
            current = current.children[0]

        while current:

            print(current.keys, end=" -> ")

            current = current.next

        print("None")