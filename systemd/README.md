# Scheduling

User-level systemd timers — no root required. Install:

```bash
mkdir -p ~/.config/systemd/user
cp systemd/*.service systemd/*.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now aerospacefunnel-poll.timer \
                             aerospacefunnel-weather.timer \
                             aerospacefunnel-disruption.timer \
                             aerospacefunnel-reference.timer \
                             aerospacefunnel-legs.timer
systemctl --user list-timers 'aerospacefunnel*'
```

Edit `WorkingDirectory=` in each unit if the checkout is not at `%h/aerospacefunnel`.

**Enable lingering** so timers run when you are not logged in — otherwise the user manager
stops at logout and every timer silently stops with it:

```bash
loginctl enable-linger $USER
```

Cadences match how fast each upstream actually changes: surveillance every minute,
weather every 10 minutes (METAR is issued hourly, oftener when conditions shift),
disruption every 5 minutes, reference data daily.

Logs: `journalctl --user -u aerospacefunnel-poll.service -f`
