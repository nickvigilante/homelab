# containerd's default container AppArmor profile, plus the upstream fix
# for containerd/containerd#14485.
#
# Ubuntu sets kernel.apparmor_restrict_unprivileged_unconfined=1, so runc's
# switch into this profile is turned into a stack, and container processes
# carry the label `cri-containerd.apparmor.d//&unconfined`.
# Stock containerd only lets a container signal and ptrace peers with the
# plain profile name, so processes in the same container can't signal each
# other.
# Kernel 7.0.0-42 enforces this for every non-privileged container
# (2026-11-04): Postgres hung after crash recovery (the startup process
# couldn't wake the checkpointer) and Authentik's gunicorn crashed with
# EACCES.
#
# containerd only generates and loads its own copy when no profile with
# this name is loaded yet.
# This file sits in /etc/apparmor.d/, so apparmor.service loads it at boot,
# before k3s starts, and containerd uses it as-is.
#
# Body: contrib/apparmor/template.go at containerd v2.2.3 (the version k3s
# v1.35.4+k3s1 bundles), rendered for gandalf (tunables/global and
# abstractions/base present, k3s unconfined, no rootlesskit).
# The only additions are the two `//&unconfined` peer rules, matching
# containerd PR #14487.
# Drop this file once a k3s release ships that fix.
#
# Managed by ansible/provision-gandalf.yml.

abi <abi/3.0>,

#include <tunables/global>

profile cri-containerd.apparmor.d flags=(attach_disconnected,mediate_deleted) {
  #include <abstractions/base>

  network,
  capability,
  file,
  umount,
  # Host (privileged) processes may send signals to container processes.
  signal (receive) peer=unconfined,
  # runc may send signals to container processes.
  signal (receive) peer=runc,
  # crun may send signals to container processes.
  signal (receive) peer=crun,
  # Manager may send signals to container processes.
  signal (receive) peer=unconfined,
  # Container processes may send signals amongst themselves.
  signal (send,receive) peer=cri-containerd.apparmor.d,
  # Same, for the stacked label (containerd/containerd#14485).
  signal (send,receive) peer=cri-containerd.apparmor.d//&unconfined,

  deny @{PROC}/* w,   # deny write for all files directly in /proc (not in a subdir)
  # deny write to files not in /proc/<number>/** or /proc/sys/**
  deny @{PROC}/{[^1-9],[^1-9][^0-9],[^1-9s][^0-9y][^0-9s],[^1-9][^0-9][^0-9][^0-9]*}/** w,
  deny @{PROC}/sys/[^k]** w,  # deny /proc/sys except /proc/sys/k* (effectively /proc/sys/kernel)
  deny @{PROC}/sys/kernel/{?,??,[^s][^h][^m]**} w,  # deny everything except shm* in /proc/sys/kernel/
  deny @{PROC}/sysrq-trigger rwklx,
  deny @{PROC}/mem rwklx,
  deny @{PROC}/kmem rwklx,
  deny @{PROC}/kcore rwklx,

  deny mount,

  deny /sys/[^f]*/** wklx,
  deny /sys/f[^s]*/** wklx,
  deny /sys/fs/[^c]*/** wklx,
  deny /sys/fs/c[^g]*/** wklx,
  deny /sys/fs/cg[^r]*/** wklx,
  deny /sys/firmware/** rwklx,
  deny /sys/devices/virtual/powercap/** rwklx,
  deny /sys/kernel/security/** rwklx,

  # allow processes within the container to trace each other,
  # provided all other LSM and yama setting allow it.
  ptrace (trace,tracedby,read,readby) peer=cri-containerd.apparmor.d,
  # Same, for the stacked label (containerd/containerd#14485).
  ptrace (trace,tracedby,read,readby) peer=cri-containerd.apparmor.d//&unconfined,
}
