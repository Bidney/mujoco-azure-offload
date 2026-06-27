"""Sample MuJoCo job for mujoco-azure-offload.

CONTRACT (this is the interface the offload tool relies on):
    run_scenario(scenario: dict, workdir: str) -> dict

  - Called once per scenario, in parallel across all cores (one process each).
  - `scenario` is one element of scenarios.json.
  - Drop any artifacts (.json/.png/.mp4/logs) into `workdir`; they are collected
    into the downloaded results archive.
  - Return a small JSON-serializable summary dict.

This sample sweeps a damped pendulum under different gravity / control / damping
settings, runs a pure-CPU rollout, writes per-step state to a JSON, plots a PNG,
and (if offscreen GL is available) renders a short MP4. Rendering is guarded so
the job still succeeds on a headless VM without a working GL stack.
"""
import json
import os

import numpy as np

MODEL_XML = """
<mujoco model="pendulum">
  <option timestep="0.002" gravity="0 0 {gravity}"/>
  <worldbody>
    <body name="pole" pos="0 0 1">
      <joint name="hinge" type="hinge" axis="0 1 0" damping="{damping}"/>
      <geom name="pole" type="capsule" fromto="0 0 0 0 0 -0.6" size="0.04" rgba="0.2 0.5 0.9 1"/>
      <body name="tip" pos="0 0 -0.6">
        <geom name="mass" type="sphere" size="0.08" rgba="0.9 0.3 0.2 1"/>
      </body>
    </body>
  </worldbody>
  <actuator>
    <motor joint="hinge" gear="1" ctrllimited="true" ctrlrange="-3 3"/>
  </actuator>
</mujoco>
"""


def run_scenario(scenario, workdir):
    import mujoco  # imported here so import errors surface per-scenario

    sid = scenario.get("id", "x")
    gravity = float(scenario.get("gravity", -9.81))
    damping = float(scenario.get("damping", 0.1))
    ctrl_amp = float(scenario.get("ctrl_amp", 0.5))
    ctrl_freq = float(scenario.get("ctrl_freq", 1.0))
    steps = int(scenario.get("steps", 3000))
    seed = int(scenario.get("seed", 0))

    rng = np.random.default_rng(seed)
    xml = MODEL_XML.format(gravity=gravity, damping=damping)
    model = mujoco.MjModel.from_xml_string(xml)
    data = mujoco.MjData(model)
    data.qpos[0] = rng.uniform(-0.3, 0.3)

    qpos = np.empty(steps, dtype=np.float64)
    qvel = np.empty(steps, dtype=np.float64)
    for t in range(steps):
        data.ctrl[0] = ctrl_amp * np.sin(2 * np.pi * ctrl_freq * data.time)
        mujoco.mj_step(model, data)
        qpos[t] = data.qpos[0]
        qvel[t] = data.qvel[0]

    summary = {
        "id": sid, "gravity": gravity, "damping": damping, "ctrl_amp": ctrl_amp,
        "steps": steps, "seed": seed,
        "final_angle": float(qpos[-1]), "max_abs_angle": float(np.max(np.abs(qpos))),
        "mean_speed": float(np.mean(np.abs(qvel))),
        "energy_proxy": float(np.mean(qvel ** 2)),
        "rendered_png": False, "rendered_mp4": False,
    }
    with open(os.path.join(workdir, "trajectory.json"), "w") as f:
        json.dump({"qpos": qpos[::20].tolist(), "qvel": qvel[::20].tolist()}, f)

    # --- optional PNG plot (matplotlib if present) ---
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(6, 3))
        ax.plot(qpos, lw=0.8)
        ax.set_title(f"scenario {sid}: pendulum angle")
        ax.set_xlabel("step"); ax.set_ylabel("angle (rad)")
        fig.tight_layout()
        fig.savefig(os.path.join(workdir, "angle.png"), dpi=90)
        plt.close(fig)
        summary["rendered_png"] = True
    except Exception as e:
        summary["png_error"] = str(e)[:200]

    # --- optional MP4 via offscreen MuJoCo rendering (needs EGL/OSMesa) ---
    try:
        import imageio.v2 as imageio
        renderer = mujoco.Renderer(model, height=240, width=320)
        data2 = mujoco.MjData(model)
        data2.qpos[0] = qpos[0]
        frames = []
        for t in range(min(steps, 600)):
            data2.ctrl[0] = ctrl_amp * np.sin(2 * np.pi * ctrl_freq * data2.time)
            mujoco.mj_step(model, data2)
            if t % 6 == 0:
                renderer.update_scene(data2)
                frames.append(renderer.render())
        imageio.mimsave(os.path.join(workdir, "rollout.mp4"), frames, fps=30)
        summary["rendered_mp4"] = True
    except Exception as e:
        summary["mp4_error"] = str(e)[:200]

    return summary
