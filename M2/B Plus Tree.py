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
