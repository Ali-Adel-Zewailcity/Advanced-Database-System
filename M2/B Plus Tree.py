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
