"""Built-in provider integration defaults.

Users only configure the provider base URL. These values describe the
currently supported page/player layout and may be updated with the plugin.
"""

DEFAULTS = {'a_hls_referer': 'https://kktv.goststream.com/kktv/index.php',
 'a_hls_template': 'https://play.arirangstream.com/live/{channel}.m3u8',
 'a_iframe_path': '/kktv/index.php',
 'a_page_path': 'live/sportstv',
 'b_cdn_hosts': ['wcdn.opolfdb.com', 'scdn.opolfdb.com', 'kcdn.opolfdb.com',
                 'wcdn.greigfd.com', 'scdn.greigfd.com', 'kcdn.greigfd.com'],
 'b_frame_match': 'fkwdsvop.com/KingKong/livescore',
 'b_hls_referer': 'https://player.fkwdsvop.com/',
 'b_page_path': 'live/sportstvb',
 'b_primary_cdn_host': 'kcdn.greigfd.com',
 'c_page_path': 'live/sportstv2',
 'c_referer_host': 'xvqz.org',
 'c_referer_path_prefix': '/content/'}
