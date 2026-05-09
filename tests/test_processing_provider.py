from nasa_earthdata.processing.algorithms import (
    CreateNormalizedDifferenceVrtAlgorithm,
    SearchEarthdataAlgorithm,
)


def test_processing_algorithm_group_does_not_duplicate_provider_name():
    algorithm = SearchEarthdataAlgorithm()

    assert algorithm.group() == "Tools"
    assert algorithm.groupId() == "tools"


def test_normalized_difference_algorithm_identity():
    algorithm = CreateNormalizedDifferenceVrtAlgorithm()

    assert algorithm.name() == "create_normalized_difference_vrt"
    assert algorithm.displayName() == "Create Normalized Difference VRT"
