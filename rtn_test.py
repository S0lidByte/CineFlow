import sys
# Add program to path
sys.path.append("/workspace/src")

from program.settings import settings_manager

# Force load
settings_manager.load()

from program.services.scrapers.shared import rtn, ranking_settings

title = "The.Blacklist.S08E09.1080p.WEB.H264-CAKES"
infohash = "dummyhash"
correct_title = "The Blacklist"

print("Ranking Settings:", ranking_settings.model_dump_json(indent=2))

active_settings = settings_manager.get_effective_rtn_model()
try:
    torrent = rtn.rank(
        raw_title=title,
        infohash=infohash,
        correct_title=correct_title,
        remove_trash=active_settings.options["remove_all_trash"],
        aliases={}
    )
    print(f"Success! Rank: {torrent.rank}")
except Exception as e:
    print(f"Rejected! Reason: {type(e).__name__} - {e}")
