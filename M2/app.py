import streamlit as st
import matplotlib.pyplot as plt
import networkx as nx

from bplustree import BPlusTree


class BPlusTreeVisualizer:

    def __init__(self):
        pass

    def visualize(self, tree):

        G = nx.DiGraph()

        labels = {}

        self._build_graph(tree.root, G, labels)

        root_id = id(tree.root)

        pos = self.hierarchy_pos(G, root_id)

        fig, ax = plt.subplots(figsize=(16, 10))

    

        for parent, child in G.edges():

            parent_x, parent_y = pos[parent]
            child_x, child_y = pos[child]

            successors = list(G.successors(parent))

            child_position = successors.index(child)

            total = len(successors)

        
            offset = (
                child_position - (total - 1) / 2
            ) * 0.04

            start_x = parent_x + offset
            start_y = parent_y - 0.03

            end_x = child_x
            end_y = child_y + 0.03

            ax.plot(
                [start_x, end_x],
                [start_y, end_y],
                color='black',
                linewidth=2
            )

    

        for node in G.nodes():

            x, y = pos[node]

            color = (
                "lightblue"
                if "Leaf" in labels[node]
                else "lightgreen"
            )

            ax.text(
                x,
                y,
                labels[node],
                ha='center',
                va='center',
                fontsize=10,
                fontweight='bold',
                bbox=dict(
                    boxstyle="round,pad=0.5",
                    facecolor=color,
                    edgecolor='black',
                    linewidth=2
                )
            )


        ax.axis("off")

        return fig


    def _build_graph(
        self,
        node,
        G,
        labels,
        parent_id=None
    ):

        node_id = id(node)

        node_type = (
            "Leaf"
            if node.leaf
            else "Internal"
        )

        labels[node_id] = f"{node_type}\n{node.keys}"

        G.add_node(node_id)

        if parent_id is not None:
            G.add_edge(parent_id, node_id)

        if not node.leaf:

            for child in node.children:

                self._build_graph(
                    child,
                    G,
                    labels,
                    node_id
                )


    def hierarchy_pos(
        self,
        G,
        root,
        width=1.0,
        vert_gap=0.2,
        vert_loc=-0.2,
        xcenter=0.5,
        pos=None
    ):

        if pos is None:
            pos = {root: (xcenter, vert_loc)}
        else:
            pos[root] = (xcenter, vert_loc)

        children = list(G.successors(root))

        if len(children) != 0:

            dx = width / len(children)

            nextx = (
                xcenter
                - width / 2
                - dx / 2
            )

            for child in children:

                nextx += dx

                pos = self.hierarchy_pos(
                    G,
                    child,
                    width=dx,
                    vert_gap=vert_gap,
                    vert_loc=vert_loc - vert_gap,
                    xcenter=nextx,
                    pos=pos
                )

        return pos


st.set_page_config(
    page_title="B+ Tree Visualizer",
    layout="wide"
)

st.title("B+ Tree Visualizer")



if 'tree' not in st.session_state:

    st.session_state.tree = BPlusTree(order=3)

btree = st.session_state.tree

visualizer = BPlusTreeVisualizer()



st.header("Insert Keys")

insert_input = st.text_input(
    "Enter keys separated by spaces",
    placeholder="Example: 5 6 7 30 40 60 1"
)

if st.button("Insert Keys"):

    try:

        values = list(
            map(int, insert_input.split())
        )

        for value in values:
            btree.insert(value)

        st.success(
            f"Inserted keys: {values}"
        )

    except:

        st.error(
            "Please enter valid integers separated by spaces."
        )


st.header("Search")

search_key = st.number_input(
    "Search Key",
    step=1,
    value=0
)

if st.button("Search"):

    found = btree.search(int(search_key))

    if found:
        st.success(
            f"Key {search_key} found."
        )
    else:
        st.error(
            f"Key {search_key} not found."
        )


st.header("Range Scan")

col1, col2 = st.columns(2)

with col1:

    start_range = st.number_input(
        "Start Range",
        step=1,
        value=0
    )

with col2:

    end_range = st.number_input(
        "End Range",
        step=1,
        value=100
    )

if st.button("Run Range Scan"):

    result = btree.range_scan(
        int(start_range),
        int(end_range)
    )

    st.info(
        f"Range Scan Result: {result}"
    )


st.header("Tree Visualization")

fig = visualizer.visualize(btree)

st.pyplot(fig)


st.header("Leaf Chain")

current = btree.root

while not current.leaf:
    current = current.children[0]

leaf_chain = []

while current:

    leaf_chain.append(str(current.keys))

    current = current.next

st.code(
    " -> ".join(leaf_chain) + " -> None"
)