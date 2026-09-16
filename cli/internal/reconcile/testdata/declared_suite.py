import pytest


@pytest.mark.lazyaf_test_id('demo.alpha')
def test_alpha():
    assert True


@pytest.mark.lazyaf_test_id('demo.beta')
def test_beta():
    raise AssertionError('never runs under --collect-only')


def test_unmarked():
    assert True
