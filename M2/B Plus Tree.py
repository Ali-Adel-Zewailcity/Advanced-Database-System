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
