"""启动器：把 src/ 加入搜索路径后启动服务。

免安装运行：`python app.py`
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / 'src'
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from leasing_agent.main import main  # noqa: E402

if __name__ == '__main__':
    main()
