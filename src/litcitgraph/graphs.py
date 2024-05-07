from typing import Self
from collections.abc import Iterator
import logging
import copy
import pickle
from pathlib import Path

from networkx import DiGraph

from litcitgraph.types import (
    ScopusID,
    DOI,
    EID,
    PybliometricsIDTypes,
    PaperProperties,
    PaperInfo,
)
from litcitgraph.requests import get_from_scopus, get_refs_from_scopus

logger = logging.getLogger('litcitgraph.graphs')
LOGGING_LEVEL = 'INFO'
logger.setLevel(LOGGING_LEVEL)


def add_cit_graph_node(
    graph: DiGraph,
    node: ScopusID,
    node_props: PaperProperties,
) -> None:
    # inplace
    if node not in graph.nodes:
        graph.add_node(node, **node_props)

def add_cit_graph_edge(
    graph: DiGraph,
    parent_node: ScopusID,
    parent_node_props: PaperProperties,
    child_node: ScopusID,
    child_node_props: PaperProperties,
    edge_weight: int | None = None,
) -> None:
    # inplace
    if parent_node not in graph.nodes:
        graph.add_node(parent_node, **parent_node_props)
    if child_node not in graph.nodes:
        graph.add_node(child_node, **child_node_props)
    
    if not graph.has_edge(parent_node, child_node):
        graph.add_edge(parent_node, child_node)
    
    if edge_weight is not None:
        # add edge weight
        graph[parent_node][child_node]['weight'] = edge_weight


class CitationGraph(DiGraph):
    
    def __init__(
        self,
        name: str = 'CitationGraph',
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        
        self._name: str = name
        self.use_doi: bool
        self.iter_depth: int = 0
        self.papers_by_iter_depth: dict[int, frozenset[PaperInfo]] = {}
        self.retrievals_total: int = 0
        self.retrievals_failed: int = 0
    
    def __repr__(self) -> str:
        return (f"CitationGraph(name={self.name}, "
                f"iter_depth={self.iter_depth}, "
                f"number of nodes: {len(self.nodes)}, "
                f"number of edges: {len(self.edges)})")
    
    @property
    def name(self) -> str:
        return self._name
    
    def deepcopy(self) -> Self:
        return copy.deepcopy(self)
    
    @staticmethod
    def prep_save(
        path: str | Path,
        suffix: str = '.pkl',
    ) -> Path:
        if isinstance(path, str):
            path = Path(path)
        path = path.with_suffix(suffix)
        return path
    
    def save_pickle(
        self,
        path: str | Path,
    ) -> None:
        path = self.prep_save(path)
        with open(path, 'wb') as f:
            pickle.dump(self, f)
    
    @classmethod
    def load_pickle(
        cls,
        path: str | Path,
    ) -> Self:
        path = cls.prep_save(path)
        with open(path, 'rb') as f:
            return pickle.load(f)
    
    def transform_graphistry(self) -> Self:
        export_graph = self.deepcopy()
        for node in export_graph.nodes:
            # Graphistry does not properly 
            # handle large integer
            export_graph.nodes[node]['scopus_id'] =\
                str(export_graph.nodes[node]['scopus_id'])
            # Graphistry drops attribute with key 'title'
            # so rename to 'paper_title'
            export_graph.nodes[node]['paper_title'] =\
                export_graph.nodes[node]['title']
            _ = export_graph.nodes[node].pop('title', None)
        
        return export_graph

    def __initialise(
        self,
        ids: Iterator[DOI | EID],
        use_doi: bool,
    ) -> None:
        """initialise citation graph with data from search query to retain
        papers which do not have any reference data

        Parameters
        ----------
        ids : Iterator[DOI | EID]
            IDs for lookup in Scopus database
        use_doi : bool
            indicator for ID type, if True DOI is used, if False EID is used

        Returns
        -------
        tuple[DiGraph, dict[IterDepth, frozenset[PaperInfo]]]
            initialised citation graph and dictionary with paper 
            information by iteration depth
        """
        self.use_doi = use_doi
        papers_init: set[PaperInfo] = set()
        
        id_type: PybliometricsIDTypes = 'doi' if use_doi else 'eid'
        
        for identifier in ids:
            # obtain information from Scopus
            paper_info = get_from_scopus(
                identifier=identifier, 
                id_type=id_type,
                iter_depth=self.iter_depth,
            )
            self.retrievals_total += 1
            
            if paper_info is None:
                self.retrievals_failed += 1
                continue
            
            node_id = paper_info.scopus_id # ScopusID as node identifier
            node_props = paper_info.graph_properties_as_dict()
            add_cit_graph_node(self, node_id, node_props)
            
            if paper_info not in papers_init:
                # verbose because duplicates should not occur as each
                # paper is unique in the database output
                # only kept to be consistent with the other methods
                papers_init.add(paper_info)
        
        self.papers_by_iter_depth[self.iter_depth] = frozenset(papers_init)
    
    def __iterate(self) -> None:
        
        target_iter_depth = self.iter_depth + 1
        papers = self.papers_by_iter_depth[self.iter_depth]
        
        papers_iteration: set[PaperInfo] = set()
        references = get_refs_from_scopus(papers, target_iter_depth)
        
        for parent, child in references:
            self.retrievals_total += 1
            if child is None:
                self.retrievals_failed += 1
                continue
            
            if (child not in papers_iteration and
                child.scopus_id not in self.nodes):
                # check if paper already in current iteration
                # or prior ones (already added to graph)
                papers_iteration.add(child)

            add_cit_graph_edge(
                graph=self, 
                parent_node=parent.scopus_id,
                parent_node_props=parent.graph_properties_as_dict(),
                child_node=child.scopus_id,
                child_node_props=child.graph_properties_as_dict(),
            )
        
        self.iter_depth = target_iter_depth
        self.papers_by_iter_depth[self.iter_depth] = frozenset(papers_iteration)
    
    def build_from_ids(
        self,
        ids: Iterator[DOI | EID],
        use_doi: bool,
        target_iter_depth: int,
    ) -> None:
        if target_iter_depth < 0:
            raise ValueError("Target depth must be non-negative!")
        elif target_iter_depth == 0:
            logger.warning(("Target depth is 0, only initialising with "
                            "given document IDs."))
        
        logger.info("Building citation graph...")
        logger.info((f"...target depth: {target_iter_depth}, "
                    f"using DOI: {use_doi}..."))
        logger.info("Initialising graph with given IDs...")
        self.__initialise(ids=ids, use_doi=use_doi)
        logger.info("Initialisation completed.")
        
        for it in range(target_iter_depth):
            logger.info(f"Starting iteration {it+1}...")
            self.__iterate()
            logger.info(f"Iteration {it+1} successfully completed.")
        
        logger.info("Building of citation graph completed.")

