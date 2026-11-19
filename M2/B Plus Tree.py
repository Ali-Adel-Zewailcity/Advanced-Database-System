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
