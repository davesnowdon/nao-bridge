import pytest

from nao_bridge_client import Trajectory


def test_portable_trajectory_roundtrip_keeps_sparse_absolute_targets():
    text = (
        '{"schema_version":"1.0","interpolation":"native","name":"head check",'
        '"keyframes":[{"time":1,"name":"left","positions":{"HeadYaw":0.2}},'
        '{"time":2,"name":"center","positions":{"HeadYaw":0}}]}'
    )
    trajectory = Trajectory.from_json(text)
    assert trajectory.keyframes[1].positions == {"HeadYaw": 0}
    assert Trajectory.from_json(trajectory.to_json()) == trajectory
    assert "HeadPitch" not in trajectory.to_json()


@pytest.mark.parametrize(
    "content",
    [
        '{"schema_version":"1.0","schema_version":"1.0"}',
        '{"schema_version":"1.0","interpolation":"native","keyframes":[{"time":NaN,"positions":{"HeadYaw":0}}]}',
        '{"schema_version":"1.0","interpolation":"native","keyframes":[{"time":1,"positions":{"HeadYaw":true}}]}',
        '{"schema_version":"1.0","interpolation":"native","keyframes":[{"time":1,"positions":{"HeadYaw":0}},{"time":1,"positions":{"HeadYaw":0.1}}]}',
        '{"schema_version":"1.0","interpolation":"native","keyframes":[]}',
        '{"schema_version":"1.0","interpolation":"native","extra":1,"keyframes":[{"time":1,"positions":{"HeadYaw":0}}]}',
    ],
)
def test_invalid_files_are_rejected(content):
    with pytest.raises(ValueError):
        Trajectory.from_json(content)
