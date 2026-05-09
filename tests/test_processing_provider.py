from nasa_earthdata.processing.algorithms import SearchEarthdataAlgorithm


def test_processing_algorithm_group_does_not_duplicate_provider_name():
    algorithm = SearchEarthdataAlgorithm()

    assert algorithm.group() == "Tools"
    assert algorithm.groupId() == "tools"
