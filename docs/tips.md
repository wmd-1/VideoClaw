#### 压缩项目

```
cd /home/wmd/projects
tar --exclude='VideoClaw/data' -czf VideoClaw.tar.gz VideoClaw
```

```
cd /home/wmd/projects
tar --exclude='VideoClaw/data' \
    --exclude='VideoClaw/.git' \
    --exclude='VideoClaw/tmp_verify' \
    --exclude='*/node_modules' \
    --exclude='*/__pycache__' \
    -czf VideoClaw.tar.gz VideoClaw
```
