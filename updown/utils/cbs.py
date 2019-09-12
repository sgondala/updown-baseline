import csv
import json
from typing import Dict, List, Optional

import anytree
import numpy as np
import torch
from torchtext.vocab import GloVe
from allennlp.data import Vocabulary


def add_constraint_words_to_vocabulary(
    vocabulary: Vocabulary, constraint_words_filepath: str, namespace: str = "tokens"
) -> Vocabulary:
    r"""
    Expand the :class:`~allennlp.data.vocabulary.Vocabulary` with CBS constraint words. We do not
    need to worry about duplicate words in constraints and caption vocabulary. AllenNLP avoids
    duplicates automatically.

    Parameters
    ----------
    vocabulary: allennlp.data.vocabulary.Vocabulary
        The vocabulary to be expanded with provided words.
    constraint_words_filepath: str
        Path to a TSV file containing two fields: first is the name of Open Images object class
        and second field is a comma separated list of words (possibly singular and plural forms
        of the word etc.) which could be CBS constraints.
    namespace: str, optional (default="tokens")
        The namespace of :class:`~allennlp.data.vocabulary.Vocabulary` to add these words.

    Returns
    -------
    allennlp.data.vocabulary.Vocabulary
        The expanded :class:`~allennlp.data.vocabulary.Vocabulary` with all the words added.
    """

    with open(constraint_words_filepath, "r") as constraint_words_file:
        reader = csv.DictReader(
            constraint_words_file, delimiter="\t", fieldnames=["class", "words"]
        )
        for row in reader:
            for word in row["words"].split(","):
                # Constraint words can be "multi-word" (may have more than one tokens).
                # Add all tokens to the vocabulary separately.
                for w in word.split():
                    vocabulary.add_token_to_namespace(w, namespace)

    return vocabulary


class ConstraintFilter(object):

    # fmt: off
    BLACKLIST: List[str] = [
        "auto part", "bathroom accessory", "bicycle wheel", "boy", "building", "clothing",
        "door handle", "fashion accessory", "footwear", "girl", "hiking equipment", "human arm",
        "human beard", "human body", "human ear", "human eye", "human face", "human foot",
        "human hair", "human hand", "human head", "human leg", "human mouth", "human nose",
        "land vehicle", "mammal", "man", "person", "personal care", "plant", "plumbing fixture",
        "seat belt", "skull", "sports equipment", "tire", "tree", "vehicle registration plate",
        "wheel", "woman"
    ]
    # fmt: on

    REPLACEMENTS: Dict[str, str] = {
        "band-aid": "bandaid",
        "wood-burning stove": "wood burning stove",
        "kitchen & dining room table": "table",
        "salt and pepper shakers": "salt and pepper",
        "power plugs and sockets": "power plugs",
        "luggage and bags": "luggage",
    }

    def __init__(self, hierarchy_jsonpath: str, nms_threshold: float = 0.85, topk: int = 3):
        def __read_hierarchy(node: anytree.AnyNode, parent: Optional[anytree.AnyNode] = None):
            # Cast an ``anytree.AnyNode`` (after first level of recursion) to dict.
            attributes = dict(node)
            children = attributes.pop("Subcategory", [])

            # Remove blacklisted object classes.
            children = [child for child in children if child["LabelName"] not in self.BLACKLIST]
            node = anytree.AnyNode(parent=parent, **attributes)
            for child in children:
                __read_hierarchy(child, parent=node)
            return node

        # Read the object class hierarchy as a tree, to make searching easier.
        self._hierarchy = __read_hierarchy(json.load(open(hierarchy_jsonpath)))

        self._nms_threshold = nms_threshold
        self._topk = topk

    def __call__(self, boxes: np.ndarray, class_names: List[str], scores: np.ndarray) -> List[str]:

        # Remove padding boxes (which have prediction confidence score = 0), and remove boxes
        # corresponding to all blacklisted classes. These will never become CBS constraints.
        keep_indices = []
        for i in range(len(class_names)):
            if scores[i] > 0 and class_names[i] not in self.BLACKLIST:
                keep_indices.append(i)

        boxes = boxes[keep_indices]
        class_names = [class_names[i] for i in keep_indices]
        scores = scores[keep_indices]

        # Perform non-maximum suppression according to category hierarchy. For example, for highly
        # overlapping boxes on a dog, "dog" suppresses "animal".
        keep_indices = self._nms(boxes, class_names)
        boxes = boxes[keep_indices]
        class_names = [class_names[i] for i in keep_indices]
        scores = scores[keep_indices]

        # Retain top-k constraints based on prediction confidence score.
        class_names_and_scores = sorted(list(zip(class_names, scores)), key=lambda t: -t[1])
        class_names_and_scores = class_names_and_scores[: self._topk]

        # Replace class name according to ``self.REPLACEMENTS``.
        class_names = [self.REPLACEMENTS.get(t[0], t[0]) for t in class_names_and_scores]

        # Drop duplicates.
        class_names = list(set(class_names))
        return class_names

    def _nms(self, boxes: np.ndarray, class_names: List[str]):
        r"""
        Perform non-maximum suppression of overlapping boxes, where the score is based on "height"
        of class in the hierarchy.
        """

        # For object class, get the height of its corresponding node in the hierarchy tree.
        # Less height => finer-grained class name => higher score.
        heights = np.array(
            [
                anytree.search.findall(self._hierarchy, lambda node: node.LabelName in c)[0].height
                for c in class_names
            ]
        )
        # Get a sorting of the heights in ascending order, i.e. higher scores first.
        score_order = heights.argsort()

        # Compute areas for calculating intersection over union. Add 1 to avoid division by zero
        # for zero area (padding/dummy) boxes.
        x1, y1, x2, y2 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
        areas = (x2 - x1 + 1) * (y2 - y1 + 1)

        # Fill "keep_boxes" with indices of boxes to keep, move from left to right in
        # ``score_order``, keep current box index (score_order[0]) and suppress (discard) other
        # indices of boxes having lower IoU threshold with current box from ``score_order``.
        # list. Note the order is a sorting of indices according to scores.
        keep_box_indices = []

        while score_order.size > 0:
            # Keep the index of box under consideration.
            current_index = score_order[0]
            keep_box_indices.append(current_index)

            # For the box we just decided to keep (score_order[0]), compute its IoU with other
            # boxes (score_order[1:]).
            xx1 = np.maximum(x1[score_order[0]], x1[score_order[1:]])
            yy1 = np.maximum(y1[score_order[0]], y1[score_order[1:]])
            xx2 = np.minimum(x2[score_order[0]], x2[score_order[1:]])
            yy2 = np.minimum(y2[score_order[0]], y2[score_order[1:]])

            intersection = np.maximum(0.0, xx2 - xx1 + 1) * np.maximum(0.0, yy2 - yy1 + 1)
            union = areas[score_order[0]] + areas[score_order[1:]] - intersection

            # Perform NMS for IoU >= 0.85. Check score, boxes corresponding to object
            # classes with smaller/equal height in hierarchy cannot be suppressed.
            keep_condition = np.logical_or(
                heights[score_order[1:]] >= heights[score_order[0]],
                intersection / union <= self._nms_threshold,
            )

            # Only keep the boxes under consideration for next iteration.
            score_order = score_order[1:]
            score_order = score_order[np.where(keep_condition)[0]]

        return keep_box_indices
