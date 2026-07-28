
# ISDBScanner

![Screenshot](https://github.com/tsukumijima/ISDBScanner/assets/39271166/a60871ed-3fb8-4a6b-9b3e-41a3ccb63362)

**受信可能な日本のテレビチャンネル (ISDB-T/ISDB-S) を全自動でスキャンし、スキャン結果を [EDCB](https://github.com/xtne6f/EDCB) ([EDCB-Wine](https://github.com/tsukumijima/EDCB-Wine))・[Mirakurun](https://github.com/Chinachu/Mirakurun)・[mirakc](https://github.com/mirakc/mirakc) の各設定ファイルや JSON 形式で出力するツールです。**

お使いの Linux PC に接続されているチューナーデバイスを自動的に検出し、全自動で受信可能なすべての地上波・BS・CS チャンネルをスキャンします。  
**実行時に `--exclude-pay-tv` オプションを指定すれば、CS と BS の有料放送をスキャン結果から除外することも可能です。**  
**さらに PC に接続されている対応チューナーを自動的に認識し、Mirakurun / mirakc のチューナー設定ファイルとして出力できます。**

地上波では、13ch 〜 62ch までの物理チャンネルをすべてスキャンして、お住まいの地域で受信可能なチャンネルを検出します。  
BS・CS では、**BS・CS1・CS2 ごとに1つの物理チャンネルのみをスキャンし TS 内のメタデータを解析することで、他のチャンネルスキャンツールよりも高速に現在放送中の衛星チャンネルを検出できます。**

> [!NOTE]  
> 日本のテレビ放送は、主に地上波 (ISDB-T) と衛星放送 (ISDB-S) の2つの方式で行われています。  
> さらに、衛星放送は BS (Broadcasting Satellite) と CS (Communication Satellite) の2つの方式があります。   
> 各放送媒体を区別するため、各媒体には一意なネットワーク ID が割り当てられています。  
>
> 地上波放送では、居住地域によって受信可能な放送局が異なる特性上、放送局ごとにネットワーク ID が割り当てられています。  
> BS 放送では、すべて同じネットワーク ID (0x0004) が割り当てられています。  
> CS 放送では歴史的経緯から、CS1 (0x0006: 旧プラット・ワン系) と CS2 (0x0007: スカイパーフェクTV!2系) で異なるネットワーク ID が割り当てられています。  
> 具体的には、物理チャンネル ND02 / ND08 / ND10 内で放送されているチャンネルは CS1 ネットワーク、それ以外は CS2 ネットワークになります。現在両者の表面的な違いはほとんどありませんが、技術的には異なるネットワークとして扱われています。
>
> BS・CS (衛星放送) では、**同一ネットワークに属するすべてのチャンネルの情報が、放送波の MPEG-2 TS 内の NIT (Network Information Table) や SDT (Service Description Table) というメタデータに含まれています。**  
> そのため、**BS・CS1・CS2 の各ネットワークごとに1つの物理チャンネルをスキャンするだけで、そのネットワークに属するすべてのチャンネルを一括で検出できます。**
> 
> さらに NIT に含まれる「現在放送中の BS/CS 物理チャンネルリスト」の情報を元にチャンネル設定ファイルを出力するため、**将来 BS 帯域再編 (トランスポンダ/スロット移動) が行われた際も、再度 ISDBScanner でチャンネルスキャンを行い、出力されたチャンネル設定ファイルを反映するだけで対応できます。**

> [!NOTE]  
> 地上波の物理チャンネルのうち 53ch - 62ch はすでに廃止されていますが、依然として一部ケーブルテレビのコミュニティチャンネル (自主放送) にて利用されているため、スキャン対象に含めています。

> [!IMPORTANT]  
> 本家 ISDBScanner は、検証環境がないため ISDB-T の C13 - C63ch (周波数変換パススルー方式) と、ISDB-C (トランスモジュレーション方式) で放送されているチャンネルのスキャンには対応していません。  
> **このフォーク ([yuta2k/ISDBScanner](https://github.com/yuta2k/ISDBScanner)) では、ケーブルテレビ (CATV) のトランスモジュレーション方式のスキャンに対応しています。詳しくは [CATV (トランスモジュレーション) 対応](#catv-トランスモジュレーション-対応) を参照してください。**

- [ISDBScanner](#isdbscanner)
  - [対応チューナー](#対応チューナー)
    - [chardev 版ドライバ](#chardev-版ドライバ)
    - [DVB 版ドライバ](#dvb-版ドライバ)
  - [対応出力フォーマット](#対応出力フォーマット)
  - [インストール](#インストール)
  - [使い方](#使い方)
    - [PC に接続されている利用可能なチューナーのリストを表示](#pc-に接続されている利用可能なチューナーのリストを表示)
    - [チャンネルスキャンを実行](#チャンネルスキャンを実行)
  - [CATV (トランスモジュレーション) 対応](#catv-トランスモジュレーション-対応)
    - [CATV 対応チューナーと dvbv5-zap のインストール](#catv-対応チューナーと-dvbv5-zap-のインストール)
    - [CATV チャンネルスキャンを実行](#catv-チャンネルスキャンを実行)
    - [出力されるファイル](#出力されるファイル)
    - [CAS カードの検出とチャンネルごとの使い分け](#cas-カードの検出とチャンネルごとの使い分け)
    - [スクリプトからの差分検知](#スクリプトからの差分検知)
    - [TSMF 分離フィルタ](#tsmf-分離フィルタ)
    - [8K マルチキャリアの並列収録](#8k-マルチキャリアの並列収録)
  - [注意事項](#注意事項)
  - [License](#license)

## 対応チューナー

[px4_drv](https://github.com/tsukumijima/px4_drv) / [smsusb (Linux カーネル標準ドライバ)](https://github.com/torvalds/linux/tree/master/drivers/media/usb/siano) 対応チューナー以外での動作は検証していませんが、おそらく動作すると思います。

> [!IMPORTANT]  
> **DVB 版ドライバを利用するには、ISDBScanner v1.1.0 / [recisdb](https://github.com/kazuki0824/recisdb-rs) v1.2.0 以降が必要です。**  
> recisdb v1.2.0 以前のバージョンは DVB 版ドライバの操作に対応していません。

### chardev 版ドライバ

- [px4_drv](https://github.com/tsukumijima/px4_drv)
  - PLEX PX-W3U4
  - PLEX PX-Q3U4
  - PLEX PX-W3PE4
  - PLEX PX-Q3PE4
  - PLEX PX-W3PE5
  - PLEX PX-Q3PE5
  - PLEX PX-MLT5PE
  - PLEX PX-MLT8PE
  - PLEX PX-M1UR
  - PLEX PX-S1UR
  - e-better DTV02A-1T1S-U
  - e-better DTV02A-4TS-P
  - e-better DTV03A-1TU
- [pt1_drv](https://github.com/stz2012/recpt1/tree/master/driver)
  - Earthsoft PT1
  - Earthsoft PT2
- [pt3_drv](https://github.com/m-tsudo/pt3)
  - Earthsoft PT3

### DVB 版ドライバ

動作検証は smsusb + VASTDTV VT20 のみ行っています。  
ほかの PX-S1UD 同等品 (Siano SMS2270 採用チューナー) シリーズであれば同様に動作するはずです。

ISDB-T / ISDB-S 対応であれば smsusb 以外のドライバ ([PT1・PT2](https://github.com/torvalds/linux/tree/master/drivers/media/pci/pt1) / [PT3](https://github.com/torvalds/linux/tree/master/drivers/media/pci/pt3) の DVB 版ドライバや [dddvb](https://github.com/DigitalDevices/dddvb) など) でも動作するはずですが、検証はできていません。  

- [smsusb (Linux カーネル標準ドライバ)](https://github.com/torvalds/linux/tree/master/drivers/media/usb/siano)
  - PLEX PX-S1UD
  - PLEX PX-Q1UD
  - MyGica S880i
  - MyGica S270 (PLEX PX-S1UD 同等品)
  - VASTDTV VT20 (PLEX PX-S1UD 同等品)

## 対応出力フォーマット

ISDBScanner は、引数で指定されたディレクトリ以下に複数のファイルを出力します。  
出力されるファイルのフォーマットは以下の通りです。

> [!NOTE]  
> **`--exclude-pay-tv` オプションを指定すると、Channels.json を除き、すべての出力ファイルにおいて有料放送チャンネルの定義が除外されます。**  
> なお、Channels.json に限り、BS 放送のみ常に有料放送チャンネルも含めた結果が出力されます (CS 放送はチャンネルスキャン処理自体が省略されるため出力されない) 。

- **Channels.json**
  - スキャン時に内部的に保持しているトランスポートストリームとサービスの情報を JSON 形式で出力します。
  - 出力される JSON のデータ構造は [constants.py](https://github.com/tsukumijima/ISDBScanner/blob/master/isdb_scanner/constants.py#L8-L77) 内の実装を参照してください。
  - BS/CS の周波数やトランスポンダ番号などかなり詳細な情報が出力されるため、スキャン結果を自作ツールなどで加工したい場合にはこのファイルを利用することをおすすめします。
- **EDCB-Wine**
  - **出力されるファイルはいずれも [EDCB-Wine](https://github.com/tsukumijima/EDCB-Wine) + [Mirakurun](https://github.com/Chinachu/Mirakurun)/[mirakc](https://github.com/mirakc/mirakc) + [BonDriver_mirakc](https://github.com/tkmsst/BonDriver_mirakc) を組み合わせた環境での利用を前提としています。**
  - EDCB のチャンネル設定ファイルには、ChSet4.txt と ChSet5.txt という2つのフォーマットがあります。
    - 両者とも中身は TSV で、各行にチャンネル設定データが記述されています。詳細なフォーマットは [formatter.py](https://github.com/tsukumijima/ISDBScanner/blob/master/isdb_scanner/formatter.py#L105-L266) の実装を参照してください。
    - ChSet4.txt には、ファイル名に対応する BonDriver ”単体” で受信可能なチャンネルの情報が記述されています。
      - **ISDBScanner で生成される ChSet4.txt は BonDriver_mirakc / BonDriver_Mirakurun 専用です。**
      - それ以外の BonDriver で使う際は、別途 ChSet4.txt 内の物理チャンネルの通し番号やチューナー空間番号の対応を変更する必要があります。
    - ChSet5.txt には、EDCB に登録されている BonDriver 全体で受信可能なチャンネルの情報が記述されています。
  - EDCB-Wine (EpgTimerSrv) のチューナー割り当て/チューナー不足判定のロジックが正常に作動しなくなる可能性があるため、**BonDriver_mirakc(_T/_S).dll のチューナー数割り当ては、Mirakurun/mirakc に登録したチューナーの数と種類 (地上波専用/衛星専用/地上波衛星共用) に合わせることを強く推奨します。**
  - **BonDriver_mirakc_T(BonDriver_mirakc).ChSet4.txt**
    - EDCB 用のチャンネル設定ファイルです。EDCB-Wine + Mirakurun/mirakc + BonDriver_mirakc の組み合わせの環境での利用を前提にしています。
    - **地上波のみのチャンネル設定データが含まれます。**
      - 別途 BonDriver_mirakc.dll を BonDriver_mirakc_T.dll にコピーすることで、**BonDriver_mirakc_T.dll を地上波専用の Mirakurun/mirakc 用 BonDriver にすることができます。**
      - PX-W3U4・PX-W3PE4 などの地上波チューナーと衛星チューナーが分かれている機種をお使いの環境では、**EpgTimerSrv のチューナー数設定で Mirakurun/mirakc に登録している地上波チューナーの数だけ BonDriver_mirakc_T.dll に割り当てることで、EDCB 上で地上波チューナーと衛星チューナーを分けて利用できます。**
  - **BonDriver_mirakc_S(BonDriver_mirakc).ChSet4.txt**
    - **BS・CS (衛星放送) のみのチャンネル設定データが含まれます。**
      - 別途 BonDriver_mirakc.dll を BonDriver_mirakc_S.dll にコピーすることで、**BonDriver_mirakc_S.dll を衛星 (BS・CS) 専用の Mirakurun/mirakc 用 BonDriver にすることができます。**
      - PX-W3U4・PX-W3PE4 などの地上波チューナーと衛星チューナーが分かれている機種をお使いの環境では、**EpgTimerSrv のチューナー数設定で Mirakurun/mirakc に登録している衛星チューナーの数だけ BonDriver_mirakc_S.dll に割り当てることで、EDCB 上で地上波チューナーと衛星チューナーを分けて利用できます。**
  - **BonDriver_mirakc(BonDriver_mirakc).ChSet4.txt**
    - **地上波・BS・CS すべてのチャンネル設定データが含まれます。**
      - このチャンネル設定ファイルを合わせて使うことで、**BonDriver_mirakc.dll を地上波・衛星 (BS・CS) 共用の Mirakurun/mirakc 用 BonDriver にすることができます。**
      - PX-MLT5PE などの地上波チューナーと衛星チューナーが統合されている機種 (マルチチューナー) をお使いの環境では、**EpgTimerSrv のチューナー数設定で Mirakurun/mirakc に登録しているマルチチューナーの数だけ BonDriver_mirakc.dll に割り当てることで、EDCB 上で適切にマルチチューナーを利用できます。**
  - **ChSet5.txt**
    - **ChSet4.txt と異なり、登録されている BonDriver のいずれかで受信可能な、地上波・BS・CS すべてのチャンネル設定データが含まれます。**
    - 各チューナー (BonDriver) に依存するチャンネル情報は ChSet4.txt の方に書き込まれます。
- **Mirakurun**
  - **channels.yml**
    - Mirakurun のチャンネル設定ファイルです。地上波・BS・CS すべてのチャンネル設定データが含まれます。
    - **`channel` プロパティに記述されている物理チャンネル名は、[recisdb](https://github.com/kazuki0824/recisdb-rs) が受け入れる物理チャンネル指定フォーマット (T13 ~ T62 / BS01_0 ~ BS23_3 / CS02 ~ CS24) に対応しています。**
      - **recpt1 の物理チャンネル指定フォーマットとは互換性がありません。**
      - recpt1 をチューナーコマンドとして使用している場合は、代わりに channels_recpt1.yml を利用してください。
  - **channels_recpt1.yml**
    - Mirakurun のチャンネル設定ファイルです。地上波・BS・CS すべてのチャンネル設定データが含まれます。
    - **`channel` プロパティに記述されている物理チャンネル名は、[recpt1](https://github.com/stz2012/recpt1) が受け入れる物理チャンネル指定フォーマット (13 ~ 62 / BS01_0 ~ BS23_3 / CS2 ~ CS24) に対応しています。**
      - **recisdb の物理チャンネル指定フォーマットとは互換性がありません。**
      - recisdb をチューナーコマンドとして使用している場合は、代わりに channels.yml を利用してください。
  - **tuners.yml**
    - Mirakurun のチューナー設定ファイルです。
    - ISDBScanner で自動検出された、PC に接続されているすべてのチューナーの情報が含まれます。
    - **`command` プロパティに記述されているチューナーコマンドには、[recisdb](https://github.com/kazuki0824/recisdb-rs) の `tune` サブコマンドが設定されています。**
      - recpt1 をチューナーコマンドとして使用している場合は、代わりに tuners_recpt1.yml を利用してください。
  - **tuners_recpt1.yml**
    - Mirakurun のチューナー設定ファイルです。
    - ISDBScanner で自動検出された、PC に接続されているすべてのチューナーの情報が含まれます。
    - **`command` プロパティに記述されているチューナーコマンドには、[recpt1](https://github.com/stz2012/recpt1) コマンドが設定されています。**
      - recisdb をチューナーコマンドとして使用している場合は、代わりに tuners.yml を利用してください。
      - recpt1 に加え、`decoder` として arib-b25-stream-test コマンドが導入されていることを前提としています。
      - recisdb と異なり recpt1 は DVB 版ドライバに対応していないため、DVB デバイスは記述から除外されます。
- **mirakc**
  - **config.yml**
    - mirakc の設定ファイルです。
    - `channels` セクションには、地上波・BS・CS すべてのチャンネル設定データが含まれます。
      - **`channel` プロパティに記述されている物理チャンネル名は、[recisdb](https://github.com/kazuki0824/recisdb-rs) が受け入れる物理チャンネル指定フォーマット (T13 ~ T62 / BS01_0 ~ BS23_3 / CS02 ~ CS24) に対応しています。**
        - **recpt1 の物理チャンネル指定フォーマットとは互換性がありません。**
        - recpt1 をチューナーコマンドとして使用している場合は、代わりに config_recpt1.yml を利用してください。
    - `tuners` セクションには、ISDBScanner で自動検出された、PC に接続されているすべてのチューナーの情報が含まれます。
      - **`command` プロパティに記述されているチューナーコマンドには、[recisdb](https://github.com/kazuki0824/recisdb-rs) の `tune` サブコマンドが設定されています。**
        - recpt1 をチューナーコマンドとして使用している場合は、代わりに config_recpt1.yml を利用してください。
  - **config_recpt1.yml**
    - mirakc の設定ファイルです。
    - `channels` セクションには、地上波・BS・CS すべてのチャンネル設定データが含まれます。
      - **`channel` プロパティに記述されている物理チャンネル名は、[recpt1](https://github.com/stz2012/recpt1) が受け入れる物理チャンネル指定フォーマット (13 ~ 62 / BS01_0 ~ BS23_3 / CS2 ~ CS24) に対応しています。**
        - **recisdb の物理チャンネル指定フォーマットとは互換性がありません。**
        - recisdb をチューナーコマンドとして使用している場合は、代わりに config.yml を利用してください。
    - `tuners` セクションには、ISDBScanner で自動検出された、PC に接続されているすべてのチューナーの情報が含まれます。
      - **`command` プロパティに記述されているチューナーコマンドには、[recpt1](https://github.com/stz2012/recpt1) コマンドが設定されています。**
        - recisdb をチューナーコマンドとして使用している場合は、代わりに config.yml を利用してください。
      - recpt1 に加え、`decode-filter` として arib-b25-stream-test コマンドが導入されていることを前提としています。
      - recisdb と異なり recpt1 は DVB 版ドライバに対応していないため、DVB デバイスは記述から除外されます。

## インストール

ISDBScanner は、チューナー受信コマンドとして [recisdb](https://github.com/kazuki0824/recisdb-rs) を利用しています。  
そのため、事前に recisdb のインストールが必要です。

> [!NOTE]  
> **[recisdb](https://github.com/kazuki0824/recisdb-rs) は、旧来から chardev 版ドライバ用チューナー受信コマンドとして利用されてきた [recpt1](https://github.com/stz2012/recpt1) と、標準入出力経由で B25 デコードを行う [arib-b25-stream-test](https://www.npmjs.com/package/arib-b25-stream-test) / [b25 (libaribb25 同梱)](https://github.com/tsukumijima/libaribb25) のモダンな代替として開発された、次世代の Rust 製チューナー受信コマンドです。**  
> 
> チューナーからの放送波の受信と B25 デコード、さらに信号レベルの確認 (checksignal) をすべて recisdb ひとつで行えます。  
> さらに recpt1 と異なり BS の物理チャンネルがハードコードされていないため、**将来 BS 帯域再編 (トランスポンダ/スロット移動) が行われた際も、recisdb を更新することなく ISDBScanner でのチャンネルスキャンと各設定ファイルの更新だけで対応できます。**

以下の手順で、recisdb をインストールしてください。  
下記は recisdb v1.2.3 時点でのインストール手順です。 

```bash
# Deb パッケージは Ubuntu 20.04 LTS / Debian 11 以降に対応

# x86_64 環境
wget https://github.com/kazuki0824/recisdb-rs/releases/download/1.2.3/recisdb_1.2.3-1_amd64.deb
sudo apt install ./recisdb_1.2.3-1_amd64.deb
rm ./recisdb_1.2.3-1_amd64.deb

# arm64 環境
wget https://github.com/kazuki0824/recisdb-rs/releases/download/1.2.3/recisdb_1.2.3-1_arm64.deb
sudo apt install ./recisdb_1.2.3-1_arm64.deb
rm ./recisdb_1.2.3-1_arm64.deb
```
> [!NOTE]  
> アンインストールは `sudo apt remove recisdb` で行えます。

ISDBScanner 自体は Python スクリプトですが、Python 3.11 がインストールされていない環境でも動かせるよう、PyInstaller でシングルバイナリ化した実行ファイルを公開しています。  
下記は ISDBScanner v1.3.3 時点でのインストール手順です。

```bash
# x86_64 環境
sudo wget https://github.com/tsukumijima/ISDBScanner/releases/download/v1.3.3/isdb-scanner -O /usr/local/bin/isdb-scanner
sudo chmod +x /usr/local/bin/isdb-scanner

# arm64 環境
sudo wget https://github.com/tsukumijima/ISDBScanner/releases/download/v1.3.3/isdb-scanner-arm -O /usr/local/bin/isdb-scanner
sudo chmod +x /usr/local/bin/isdb-scanner
```

## 使い方

![Screenshot](https://github.com/user-attachments/assets/ff759d35-b902-47f6-aeb4-18e5949bee30)

ISDBScanner は、引数で指定されたディレクトリ (デフォルト: `./scanned/`) 以下に複数のファイルを出力します。  
出力される各ファイルのフォーマットは [対応出力フォーマット](#対応出力フォーマット) を参照してください。

> [!IMPORTANT]
> **重要: ISDBScanner v1.3.0 以降を recisdb + px4_drv 環境で使う場合は、[tsukumijima/px4_drv](https://github.com/tsukumijima/px4_drv) かつ v0.4.0 以降に更新する必要があります。**  
> ISDBScanner v1.3.0 からは、px4_drv の chardev 版デバイスと全ての DVB 版デバイスで、**BS チャンネルを物理チャンネル番号（スロット番号・相対 TS 番号）ではなく、TSID (Transport Stream ID) で選局するように変更されました。**  
> TSID で選局することで、2025年2月末の帯域再編のように放送局側が通知なしに相対 TS 番号を変更した場合でも、受信できなくなる問題を防げます。  
> ただし、オリジナルの [nns779/px4_drv](https://github.com/nns779/px4_drv) は TSID での選局に対応していないため、**ISDBScanner が生成した設定ファイルを使うには、px4_drv をフォーク版である [tsukumijima/px4_drv](https://github.com/tsukumijima/px4_drv) に更新する必要があります。**  
> なお、PT1/PT2/PT3 の chardev 版ドライバは TSID での選局に対応していないため、これらのチューナー向けには TSID 選局の設定は生成されません。  
> また、[stz2012/recpt1](https://github.com/stz2012/recpt1) は TSID での選局に対応していないため、recpt1 向けの設定ファイルでは TSID での選局は行いません。

> [!TIP]
> **ISDBScanner v1.2.0 以降では、`--lnb` オプションを指定すると、衛星放送受信時にチューナーからアンテナに給電できます（動作未確認）。**  
> `--lnb 11v` と `--lnb 15v` の両方を指定できますが、px4_drv 対応チューナーには `--lnb 15v` のみ指定できます。  
> 明示的に LNB 給電を無効化するには、`--lnb low` を指定します。何も指定されなかったときは LNB 給電を行いません。

### PC に接続されている利用可能なチューナーのリストを表示

![Screenshot](https://github.com/tsukumijima/ISDBScanner/assets/39271166/99a9fcd4-0afb-4c42-914a-d284fb3cf057)

`isdb-scanner --list-tuners` と実行すると、PC に接続されている、利用可能なチューナーのリストが表示されます。  
チューナーが現在使用中の場合、チューナー情報の横に `(Busy)` と表示されます。

PC に接続したはずのチューナーが認識されていない場合は、チューナードライバのインストール・ロード状態や、チューナーとの物理的な接続状況を確認してみてください。

> [!NOTE]  
> チューナーは chardev 版デバイスが先に認識され、DVB 版デバイスは後に認識されます。  
> chardev 版デバイスと DVB 版デバイスが同時に接続されている場合、chardev 版デバイスの方を優先してチャンネルスキャンに使用します。

### チャンネルスキャンを実行

地上波・BS・CS すべてのチャンネルをスキャンする際は、`isdb-scanner` と実行してください。  
出力先ディレクトリを指定しない場合は `./scanned/` に出力されます。  

地上波と BS の無料放送のみをスキャン結果に含めたい場合は、`isdb-scanner --exclude-pay-tv` と実行してください。

<img align="center" width="49%" src="https://github.com/tsukumijima/ISDBScanner/assets/39271166/d54dd1c9-0ad8-40a0-9678-60f5a1ea8fc6">
<img align="center" width="49%" src="https://github.com/tsukumijima/ISDBScanner/assets/39271166/59368ddb-38b4-40fe-9c7f-7aee39828d13">
<br><br>

**チャンネルスキャン中は、検出されたトランスポートストリーム / チャンネル (サービス) のリストとスキャンの進捗状況が、リアルタイムでグラフィカルに表示されます。**  
チャンネルスキャンに使おうとしたチューナーが現在使用中の際は、自動的に空いているチューナーを選択してスキャンを行います。  
もし地上波で特定のチャンネルが受信できていない場合は、停波中でないかや受信状態などを確認してみてください。

なお、チャンネルスキャンには地デジ・BS・CS のフルスキャンを行う場合で 6 分程度、地デジ・BS の無料放送のみをスキャンする場合で 5 分半程度かかります。  
コマンドを実行した後は終わるまで放置しておくのがおすすめです。

> [!IMPORTANT]  
> **地上波で複数の中継局の電波を受信できる地域にお住まいの場合、同一のチャンネルが重複して検出されることがあります。**  
> この場合、ISDBScanner は同一のチャンネルを放送している各物理チャンネルごとに信号レベルを計測し、最も受信状態の良い物理チャンネルのみを選択します。動作確認はできていないけどおそらく動くはず…？  

> [!NOTE]  
> 出力される Mirakurun / mirakc のチューナー設定ファイルには、現在 PC に接続中のチューナーのみが記載されます。  
> 接続しているはずのチューナーが記載されない (ISDBScanner で認識されていない) 場合は、カーネルドライバのロード状態や、物理的なチューナーの接続状態を確認してみてください。

## CATV (トランスモジュレーション) 対応

> [!NOTE]  
> **このセクションで説明する機能は、フォーク ([yuta2k/ISDBScanner](https://github.com/yuta2k/ISDBScanner)) で追加した独自機能です。本家 ISDBScanner には含まれていません。**

日本のケーブルテレビ (CATV) で使われる **トランスモジュレーション方式 (ISDB-C / ITU-T J.83 Annex C・64QAM)** のチャンネルスキャンに対応しています。  
地上波・BS・CS の通常スキャン (`isdb-scanner`) とは独立した別コマンド群として実装しているため、**既存のスキャン機能やその出力には一切影響しません。**

主な機能:

- **TSMF (JCTEA STD-002) 多重の分離**
  - CATV では、1つの物理チャンネル (RF) に複数の TS を多重して伝送する「トランスモジュレーション」が使われることがあります。この多重フレーム (TSMF) を解析し、多重されている相対 TS (1〜15) ごとに分離してスキャンします。
- **CAS 種別の判定**
  - 各チャンネルのスクランブル状態 (PMT/CAT の CA 記述子・スクランブル率・SDT の有料放送フラグ) を解析し、視聴に必要なカード種別 (**カード不要 / B-CAS / C-CAS / A-CAS**) を判定して出力します。
- **再送信元の判定**
  - 各 TS が地上波・BS・CS の再送信か、CATV 局の自主放送 (コミュニティチャンネル) かを、ネットワーク ID から判定します。
- **4K/8K (MMT/TLV) 放送の解析**
  - 高度 BS デジタル放送 (4K/8K) を再送信している MMT/TLV チャンネルから、サービス・アセット構成やネットワーク情報 (TLV-NIT) を JSON に出力します。**シグナリング情報は非スクランブルのため、ACAS カードなしで解析できます。**
  - MH-SDT (ARIB STD-B60) も解析し、各 MMT サービスのサービス名 (`service_name`) を取得します。MH-SDT には他ストリームのサービスも記載されるため、1 チャンネルの受信データから同じ放送網内のサービス一覧 (`sdt_services`) も併せて出力します。
- **8K マルチキャリア分散伝送の検出**
  - 8K 放送が複数の物理チャンネルに分割して伝送されている場合、TSMF ヘッダの拡張情報や TLV-NIT の周波数リストから、そのグループ構成 (どの物理チャンネルが同じ 8K 放送の一部か) を検出します。
  - 同じグループのキャリアは 1 本の TLV ストリームを分散伝送しているだけで、シグナリング (MPT / MH-SDT / TLV-NIT) は各キャリアに断片的にしか届かないため、**グループ内でシグナリング情報をマージしてから出力します。** これにより、グループを構成する全キャリアで同一のサービス一覧が出力され、定期スキャン時の差分レポートに偽陽性の増減が出ることもなくなります。
- **BS/CS 再送信チャンネルの type マッピングとチャンネル名の解決**
  - BS/CS の再送信 (トランスモジュレーション) チャンネルは、Mirakurun/mirakc のチャンネル設定に `type: GR` 固定ではなく `type: BS` / `type: CS` として出力します。チャンネル名には、TSMF 分離後の SDT から取得した映像サービス名 (例: 「ＢＳ朝日」) を使用します。
- **ネイティブ地上波/BS/CS スキャンの統合** (`--terrestrial` / `--satellite` 指定時)
  - ISDB-T / ISDB-S 対応チューナーと recisdb が利用可能な場合、CATV スキャンの完了後にネイティブ地上波 (ISDB-T) / BS/CS (ISDB-S 衛星アンテナ直結) のチャンネルスキャンも実行し、Mirakurun/mirakc 向けのチャンネル設定に CATV 分と地上波/BS/CS 分を統合して出力します (デフォルト: いずれも無効)。CATV パススルー/トランスモジュレーション + 地上波・衛星アンテナ併用環境で、1 回のスキャンでレコーダー設定一式を生成できます。
  - スキャンは CATV → 地上波 → 衛星の順に直列実行されます。CATV 再送信とネイティブで同一 TS が重複する場合は、`--prefer catv` / `--prefer native` でどちらを有効にするかを自動調整できます (もう一方のエントリは無効化して出力)。
- **チューナー設定 (tuners) / EDCB 設定の出力**
  - Mirakurun/mirakc 向けのチューナー設定 (`tuners_catv.yml`) と、EDCB (EDCB-Wine) 向けのチャンネル設定 (`EDCB-Wine/`) も合わせて出力します (EDCB 出力は実機検証未了の実験的機能です)。
- **既存 JSON からの再フォーマット** (`--from-json` 指定時)
  - スキャンを行わず、出力先に残っている既存の JSON (`CATV.json` 必須 + `Terrestrial.json` / `BS.json` / `CS.json` は任意) から、出力ファイル群 (dvbv5 conf / Mirakurun・mirakc の channels/tuners / EDCB) だけを再生成します。オプション (`--exclude-pay-tv` など) を変えて設定だけ作り直したいときに使えます。

> [!IMPORTANT]  
> **CATV 対応では、チューナー受信コマンドとして recisdb ではなく [dvbv5-zap](https://www.linuxtv.org/wiki/index.php/Dvbv5-zap) (DVBv5 Tools / dvb-tools) を使用します。**  
> recisdb は ISDB-C (トランスモジュレーション) に対応していないためです。  
> そのため、**ITU-T J.83 Annex C 対応の DVB 版チューナーが必要です。** 動作検証は [Digital Devices Max M4](https://www.digital-devices.eu/) で行っています。

> [!NOTE]  
> **ISDBScanner (本フォーク含む) は、スクランブルされた放送のデコードは行いません。**  
> CATV スキャンは、放送波に含まれる非スクランブルのメタデータ (PAT / NIT / SDT / MMT シグナリングなど) のみを解析します。  
> B-CAS / C-CAS / A-CAS カードによるスクランブル解除は、Mirakurun / mirakc / EDCB など実際に受信・録画を行うソフト側で行ってください。

### CATV 対応チューナーと dvbv5-zap のインストール

CATV スキャンには、DVB-C (ITU-T J.83 Annex C) 対応の DVB 版チューナーと、`dvbv5-zap` コマンドが必要です。  
`dvbv5-zap` は、Debian / Ubuntu では以下のようにインストールできます。

```bash
sudo apt install dvb-tools
```

CATV 対応の各コマンド (`isdb-catv-scanner` / `isdb-tsmf-split` / `isdb-catv-capture`) は、`catv` ブランチのソースコードから利用します。

```bash
# ソースコードからの実行 (Python 3.11 以降 + Poetry が必要)
git clone -b catv https://github.com/yuta2k/ISDBScanner.git
cd ISDBScanner
poetry install
poetry run isdb-catv-scanner --list-tuners
```

> [!NOTE]  
> GitHub Actions のビルドワークフローでは、通常の `isdb-scanner` に加え、`isdb-catv-scanner` / `isdb-tsmf-split` / `isdb-catv-capture` のシングルバイナリもビルドされます。

### CATV チャンネルスキャンを実行

利用可能な CATV 対応チューナーを確認するには、`isdb-catv-scanner --list-tuners` を実行します。

CATV チャンネルスキャンを実行するには、`isdb-catv-scanner` を実行します。  
出力先ディレクトリを指定しない場合は `./scanned/` に出力されます。

```bash
# すべての CATV 物理チャンネル (CATV_13〜62 / CATV_C13〜C63) をスキャン
poetry run isdb-catv-scanner ./scanned/

# 特定の物理チャンネルのみをスキャン
poetry run isdb-catv-scanner --channels CATV_15,CATV_C36 ./scanned/
```

主なオプション:

- **`--channels <物理チャンネル,...>`**: スキャン対象の物理チャンネルをカンマ区切りで指定します (例: `CATV_15,CATV_C36`)。省略時はすべての CATV 物理チャンネルをスキャンします。
- **`--adapter <番号>`**: 使用する DVB アダプタ番号を指定します。省略時は検出された CATV 対応チューナーの先頭を使用します。
- **`--recording-time <秒>`**: 各物理チャンネルの受信時間 (秒) を指定します (デフォルト: 10 秒)。
- **`--tlv-recording-time <秒>`**: TLV (4K/8K MMT) キャリアと判定された物理チャンネルのみに適用する、合計の受信時間 (秒) を指定します (デフォルト: 20 秒)。
  - TLV キャリアの MH-SDT には放送網内の全ストリーム分のサービス情報がセクション分割で載っており、10 秒程度の受信では全セクションが揃わずスキャンごとに取得できるサービス数がばらつきます (実測では 20 秒受信すれば毎回すべて揃います)。そのため、TLV キャリアだけ受信時間を自動的に延長します。
  - 判定は受信中に行われ、`--recording-time` 秒の時点で TLV キャリアでないと分かったチャンネルはその時点で受信を打ち切るため、TSMF/SingleTS キャリアのスキャン時間は従来どおりです (選局はやり直さず、そのまま受信を続けて延長します)。
  - `--recording-time` 以下の値を指定した場合は延長を行いません (エラーにはなりません)。
- **`--collect-signal-stats` / `--no-collect-signal-stats`**: 選局中に信号強度・CNR・ビットエラーレートを取得して結果に含めます (デフォルト: 有効)。
- **`--no-diff`**: 出力先に既存の `CATV.json` があっても、前回スキャンとの差分レポートを生成しません。
- **`--fail-on-diff`**: スキャンと出力ファイルの生成が正常に完了した上で、前回スキャンからチャンネル構成に変化があった場合に終了コード 2 で終了します (デフォルト: 無効)。スクリプトから変化の有無を検知するためのオプションです。
  - 終了コードの意味は **0 = 変化なし / 1 = スキャン失敗 (チューナーが見つからない・全チューナーが使用不能になったなど) / 2 = 変化あり** です。終了コード 2 の場合も、出力ファイル一式は通常どおり生成されます。
  - 前回のスキャン結果 (`CATV.json`) が存在しない初回実行では、差分自体が生成されないため「変化あり」扱いにはならず、終了コード 0 になります。
  - 信号品質の劣化警告 (後述) は測定ごとに揺れる値のため、終了コードには影響しません。
  - `--no-diff` とは指定意図が矛盾するため、併用するとエラー (終了コード 1) になります。
- **`--list-card-readers`**: PC に接続されている PC/SC カードリーダーと、それぞれに挿さっている CAS カードの種別 (B-CAS / C-CAS)・CA_system_id・カード ID の一覧を表示して終了します (スキャンは行いません)。表示されるリーダー名は、recisdb の `--card` オプションにそのまま渡せる文字列です。
- **`--check-cards` / `--no-check-cards`**: スキャン完了後に接続されている CAS カードを検出し、スキャン結果と突合した受信可能性レポート (`CATV.cards.txt`) を出力します (デフォルト: 有効)。B-CAS カードと C-CAS カードの両方が検出された場合は、チャンネルごとにカードを使い分けるためのデコーダースクリプトも生成します (後述)。
  - カード検出には pcsc-lite (pcscd) が必要ですが、pcscd が起動していない・リーダーが接続されていない場合でもスキャン自体は正常終了し、レポートに理由が記載されるだけです。
  - カードには照会コマンド (ARIB STD-B25 の Initial Setting Conditions) を 1 回送るだけで、共有モードで接続するため、録画などで使用中のカードに対しても安全に実行できます。
- **`--satellite` / `--no-satellite`**: CATV スキャンの完了後に、ネイティブ BS/CS (衛星アンテナ直結・ISDB-S) のチャンネルスキャンも実行し、Mirakurun/mirakc 向けのチャンネル設定に BS/CS 分を統合して出力します (デフォルト: 無効)。
  - ネイティブ BS/CS のスキャンには [recisdb](https://github.com/kazuki0824/recisdb-rs) と ISDB-S 対応チューナーが必要です。どちらかが見つからない場合は警告を表示して衛星スキャンのみを自動的にスキップし、CATV スキャンは通常どおり実行されます。
  - CATV 再送信の BS/CS チャンネルとネイティブ BS/CS チャンネルはどちらも `type: BS` / `type: CS` になるため、併用時は Mirakurun/mirakc が type からチューナー (dvbv5-zap / recisdb) を区別できません。両方が検出された場合は警告を表示するので、`--prefer catv` / `--prefer native` で自動調整するか、生成された設定でどちらか一方を無効化するなどの調整をしてください。
- **`--terrestrial` / `--no-terrestrial`**: CATV スキャンの完了後に、ネイティブ地上波 (ISDB-T) のチャンネルスキャン (T13〜T62 のフルスキャン) も実行し、Mirakurun/mirakc 向けのチャンネル設定に地上波 (`type: GR`) 分を統合して出力します (デフォルト: 無効)。
  - ネイティブ地上波のスキャンには [recisdb](https://github.com/kazuki0824/recisdb-rs) と ISDB-T 対応チューナーが必要です。どちらかが見つからない場合は警告を表示して地上波スキャンのみを自動的にスキップし、CATV スキャンは通常どおり実行されます。
- **`--prefer <catv|native>`**: CATV 再送信とネイティブ (recisdb) で同一 TS (同一の放送種別・TSID) が重複したとき、指定した側のエントリを有効なまま残し、もう一方を Mirakurun の `isDisabled: true` / mirakc の `disabled: true` として出力します。省略時は両方を有効なまま出力し、重複が検出された場合は警告を表示します。
- **`--normalize-names`**: Mirakurun/mirakc/EDCB のチャンネル名に含まれる全角英数字・記号を半角に正規化して出力します (放送局側で英数字が全角符号化されている場合の見づらさを緩和する任意処理)。デフォルト: 無効。
- **`--from-json`**: 再フォーマットモード。チューナー検出もスキャンも行わず、出力先の既存 JSON (`CATV.json` 必須) から出力ファイル群 (dvbv5 conf / Mirakurun・mirakc の channels/tuners / EDCB) だけを再生成します。`CATV.json` が見つからない場合はエラー終了します。スキャン関連オプション (`--channels` / `--adapter` / `--satellite` / `--terrestrial` など) は無視され、出力系オプション (`--exclude-pay-tv` / `--bcas-only` / `--cas-as-sky` / `--prefer` / `--normalize-names`) のみが有効です。JSON 群と diff は再生成しません。
- **`--exclude-pay-tv`**: Mirakurun/mirakc 向けのチャンネル設定から有料放送チャンネルを除外します (CATV エントリ・ネイティブ地上波/BS/CS エントリの両方に適用)。`--satellite` 指定時は CS のスキャン自体も省略します。JSON 出力 (`CATV.json` / `BS.json`) には常に全チャンネルが出力されます。
- **`--bcas-only`**: Mirakurun/mirakc 向けのチャンネル設定を、B-CAS カードで受信可能なチャンネルのみに限定します (C-CAS/A-CAS が必要なチャンネルと、スクランブルされているが CAS 種別を特定できないチャンネルを除外します)。デフォルト: 無効。
- **`--cas-as-sky`**: C-CAS/A-CAS が必要なチャンネルを、Mirakurun/mirakc のチャンネル設定に `type: SKY` として出力します (本来は SPHD 用の第4の type を CATV 専用チャンネルの分離に転用するオプション)。デフォルト: 無効。
- **`--lnb <11v|15v|low>`**: ネイティブ BS/CS のスキャン時の LNB 給電電圧を指定します (デフォルト: `low` = 給電なし)。
- **`--output-recisdb-log`**: ネイティブ BS/CS のスキャン時に recisdb のログを標準エラー出力に出力します。

> [!NOTE]  
> 物理チャンネル名は、[日本の CATV で一般的な周波数プラン](https://github.com/yuta2k/ISDBScanner/blob/catv/isdb_scanner/catv/constants.py) に基づき、UHF 帯 (`CATV_13`〜`CATV_62`) と下位バンド (`CATV_C13`〜`CATV_C63`) を定義しています。  
> DELIVERY_SYSTEM は `DVBC/ANNEX_A`、SYMBOL_RATE は 5,274,000、MODULATION は `QAM/AUTO` を使用します。

### 出力されるファイル

CATV スキャンは、指定されたディレクトリ以下に以下のファイルを出力します。

- **CATV.json**
  - 各物理チャンネル (キャリア) の解析結果を、物理チャンネル名をキーとした JSON 形式で出力します。
  - キャリア種別 (TSMF / SingleTS / TLV / Empty)、多重されている各 TS の TSID・ネットワーク情報・サービス一覧・CAS 種別・再送信元・信号品質、および MMT/TLV チャンネルのサービス/アセット構成・8K マルチキャリアグループ情報が含まれます。
  - MMT/TLV チャンネルでは、MH-SDT から取得したサービス名が各 MMT サービスの `service_name` (`service_id` 付き) に反映されるほか、放送網内のサービス一覧が `sdt_services` (サービス名・事業者名・サービス形式種別・無料/有料・自ストリームかどうか) として出力され、`service_id` とサービス名でのチャンネル特定が可能になります。
- **CATV.diff.txt**
  - 出力先に前回の `CATV.json` が存在する場合のみ生成されます。
  - 前回スキャンからのチャンネル・TS・サービス・CAS 種別・再送信元の増減/変化を、`+` / `-` / `~` のプレフィックス付きのプレーンテキストで出力します。CATV はヘッドエンドの構成変更が起きやすいため、再スキャン時の差分確認に利用できます。
  - 前回スキャンから CNR が 5.0 dB 以上低下した物理チャンネルがある場合は、レポートの先頭に `Signal degradation warnings:` セクション (行頭 `!`) を出力し、あわせてコンソールにも警告を表示します。分配器・ケーブルの劣化やヘッドエンド側のレベル変更に気付くための目安で、チャンネル構成の変化とは別扱いです。
- **CATV.diff.json**
  - `CATV.diff.txt` と同じ内容を機械可読な JSON で出力したものです。テキスト版と必ず同時に生成されます。
  - トップレベルに `has_changes` (チャンネル構成に変化があったか) と `signal_warnings` (信号品質の劣化警告) を含むため、スクリプトから `jq` 1 発で差分の有無を判定できます。`signal_warnings` は `has_changes` には含まれません。
- **CATV.cards.txt** (`--check-cards` 有効時のみ)
  - 検出された PC/SC カードリーダー・CAS カードの一覧と、スキャン結果 (各 TS の `required_card`) を突合した受信可能性レポートです。
  - チャンネル (TS) ごとに、受信に必要なカード種別・手持ちのカードで復号できるか (`yes` / `no` / `unknown`)・使用すべきカードリーダー名を出力します。
  - C-CAS チャンネルの `yes` は「その CATV 局が発行した契約済み C-CAS カードであること」が前提です (カードリーダーからは契約状態まで判別できません)。また、A-CAS (4K/8K) は CA_system_id が B-CAS と同じ 0x0005 でリーダーからは区別できないため、常に `unknown` になります。
- **Mirakurun/decoder-bcas.sh / Mirakurun/decoder-ccas.sh / mirakc/decode-filter.sh** (B-CAS と C-CAS の両方が検出された場合のみ)
  - チャンネルごとに B-CAS / C-CAS カードを使い分けるためのデコーダースクリプトです。詳細は[後述](#cas-カードの検出とチャンネルごとの使い分け)。
- **dvbv5_channels_catv.conf**
  - 受信できた (ロックに成功した) チャンネルのみを収録した、dvbv5 形式のチャンネル設定ファイルです。`dvbv5-zap -c dvbv5_channels_catv.conf ...` でそのまま選局に利用できます。
- **Terrestrial.json / BS.json / CS.json** (ネイティブ地上波/BS/CS スキャン実行時のみ)
  - ネイティブ地上波/BS/CS のチャンネルスキャン解析結果を、通常の `isdb-scanner` が出力する `Channels.json` の `Terrestrial` / `BS` / `CS` キーと同じ形式 (TS 情報の JSON 配列) で出力します。
  - `Terrestrial.json` は `--terrestrial`、`BS.json` / `CS.json` は `--satellite` を指定した場合のみ出力されます。
  - `--exclude-pay-tv` の指定に関わらず、JSON には取得できた全チャンネルが出力されます (`--exclude-pay-tv` 指定時は CS のスキャン自体を行わないため、`CS.json` は出力されません)。
- **Terrestrial.diff.txt / BS.diff.txt / CS.diff.txt** (対応するネイティブスキャン実行時 + 前回の JSON が存在する場合のみ)
  - 前回の `Terrestrial.json` / `BS.json` / `CS.json` と今回のスキャン結果の差分 (物理チャンネル・TSID・サービスの増減/変化) を、`+` / `-` / `~` のプレフィックス付きのプレーンテキストで出力します。`--no-diff` 指定時は生成されません。
- **Mirakurun/channels_catv.yml**
  - Mirakurun 用のチャンネル設定 (CATV 部分 + ネイティブ地上波/BS/CS 部分) です。
  - **Mirakurun は TSMF 分離をネイティブサポートしている ([`tsmfRelTs`](https://github.com/Chinachu/Mirakurun/blob/master/doc/Configuration.md) プロパティ)** ため、TSMF 多重チャンネルには `tsmfRelTs` キーを付与して出力します。ファイル冒頭のコメントに、`dvbv5-zap` を使う `tuners.yml` の記述例を記載しています。
  - CATV エントリの `type` は再送信元に応じて `GR` (地上波再送信・自主放送) / `BS` / `CS` になります (`--cas-as-sky` 指定時は C-CAS/A-CAS が必要なチャンネルのみ `SKY`)。**CATV チューナーの `types` には、このファイルに現れる全 type を列挙してください** (例: `[GR, BS, CS]`)。
  - ネイティブ地上波/BS/CS のスキャン (`--terrestrial` / `--satellite`) を実行した場合は、CATV エントリの後にネイティブ地上波 (`type: GR`) / BS/CS のチャンネルエントリ (recisdb で選局・`satellite` キーで TSID 指定) も統合して出力します。ISDB-T/ISDB-S チューナー用の `tuners.yml` の記述例もファイル冒頭のコメントに記載しています。
- **mirakc/channels_catv.yml**
  - mirakc 用のチャンネル設定 (CATV 部分 + ネイティブ地上波/BS/CS 部分) です。
  - **mirakc は TSMF 分離に対応していない**ため、チューナーコマンドのパイプに `isdb-tsmf-split --rel-ts <番号>` を挟む形の設定例を出力します。
  - CATV エントリの `type` は Mirakurun と同様に `GR` / `BS` / `CS` (+ `--cas-as-sky` 時は `SKY`) になります。CATV チューナーの `types` にはこのファイルに現れる全 type を列挙してください。
  - ネイティブ地上波/BS/CS のスキャン (`--terrestrial` / `--satellite`) を実行した場合は、CATV エントリの後にネイティブ地上波 (`type: GR`) / BS/CS のチャンネルエントリ (recisdb で選局・`extra-args` キーで TSID 指定) も統合して出力します。
- **Mirakurun/tuners_catv.yml / mirakc/tuners_catv.yml**
  - Mirakurun / mirakc 用のチューナー設定です。検出された CATV チューナー (dvbv5-zap) と、ネイティブスキャンに使った ISDB-T/ISDB-S チューナー (recisdb) を列挙します。
  - CATV チューナーの `types` には、`channels_catv.yml` に実際に現れる CATV エントリの全 type (`GR` / `BS` / `CS`、`--cas-as-sky` 時は `SKY`) が自動的に列挙されます。
- **EDCB-Wine/** (実験的機能)
  - EDCB (EDCB-Wine) 向けのチャンネル設定 (`BonDriver_mirakc(BonDriver_mirakc).ChSet4.txt` / `ChSet5.txt`) を出力します。ネイティブスキャンを実行した場合はネイティブ地上波/BS/CS 分も統合されます。
  - **実機での動作検証は未了の実験的出力です。** 実運用に使う際は生成された内容を確認してください。

### CAS カードの検出とチャンネルごとの使い分け

CATV のトランスモジュレーション再送信では、地上波/BS/CS の再送信チャンネルは元の B-CAS スクランブルのまま再多重されている一方、CATV 自主放送や CATV 専門チャンネルは事業者が発行する C-CAS カードでスクランブルされています。  
このため、CATV の全チャンネルを 1 台のレコーダーで扱うには、B-CAS カードと C-CAS カードをチャンネルごとに使い分ける必要があります。

まず、接続されているカードリーダーとカード種別を確認できます。

```bash
isdb-catv-scanner --list-card-readers
```

スキャンを実行すると (`--check-cards` はデフォルトで有効)、受信可能性レポート `CATV.cards.txt` が出力され、B-CAS と C-CAS の両方が検出された場合は以下のデコーダースクリプトが生成されます。いずれも、カードリーダー名を指定して復号できる [recisdb](https://github.com/kazuki0824/recisdb-rs) (`recisdb decode --card '<リーダー名>'`) を使用します (arib-b25-stream-test は Linux ではカードリーダーを選択できないため、複数カード環境では使えません)。

- **Mirakurun**: Mirakurun の `decoder` はチューナー単位でしか指定できず、コマンドに引数も渡せないため、リーダー名を埋め込んだラッパースクリプト `Mirakurun/decoder-bcas.sh` / `Mirakurun/decoder-ccas.sh` を生成します。チャンネルごとの使い分けには、`--cas-as-sky` で C-CAS チャンネルを `type: SKY` に分離した上で、`decoder` に `decoder-ccas.sh` を指定した SKY 専用のチューナーエントリを追加してください (記述例は生成される `tuners_catv.yml` のコメントに記載しています)。
  - **SKY 専用エントリと B-CAS 用エントリで同じ物理アダプタを共有しないでください。** Mirakurun はチューナーエントリ間でアダプタの重複を検査しないため、同じアダプタを指す 2 つのエントリが同時に選局され、dvbv5-zap が二重起動して両方失敗することがあります。
- **mirakc**: mirakc の `filters.decode-filter.command` はチャンネルごとにテンプレート展開されるため、生成される `mirakc/decode-filter.sh` を指定するだけでチャンネルごとの使い分けができます (`--cas-as-sky` は不要です)。組み込み例はスクリプト冒頭のコメントに記載しています。C-CAS が必要な物理チャンネルのリストはスキャン結果からスクリプトに埋め込まれるため、局側の構成変更後は再スキャンで再生成してください。

なお、1 枚のカードは複数チューナーの同時録画で共有できます (pcsc-lite は共有モードの接続をリーダー単位で直列化するため、複数の recisdb プロセスから同じカードを同時に使っても問題ありません)。

### スクリプトからの差分検知

`--fail-on-diff` を指定すると、**0 = 変化なし / 1 = スキャン失敗 / 2 = 変化あり** の終了コードで結果を受け取れます。  
チャンネル構成の変化だけを通知したい場合は、終了コードで分岐するのが手軽です。

```bash
# スキャンし、チャンネル構成に変化があったときだけ通知する
isdb-catv-scanner --fail-on-diff /var/lib/isdb-catv/scanned/
case $? in
  2) notify.sh "$(cat /var/lib/isdb-catv/scanned/CATV.diff.txt)" ;;
  1) notify.sh "CATV スキャンに失敗しました" ;;
esac
```

`CATV.diff.json` を使えば、終了コードに頼らず `jq` だけで判定することもできます。

```bash
isdb-catv-scanner /var/lib/isdb-catv/scanned/
DIFF=/var/lib/isdb-catv/scanned/CATV.diff.json
# チャンネル構成の変化 (信号品質の劣化警告は has_changes に含まれない)
[ -f "$DIFF" ] && jq -e '.has_changes' "$DIFF" > /dev/null && notify.sh "CATV のチャンネル構成が変化しました"
# CNR が 5.0 dB 以上低下した物理チャンネル (変化なしでも出力されることがある)
[ -f "$DIFF" ] && jq -r '.signal_warnings[] | "\(.physical_channel): -\(.drop_db) dB"' "$DIFF"
```

> [!NOTE]  
> スキャン中は CATV チューナーを占有します。レコーダー (Mirakurun / mirakc / EDCB) と同じチューナーを使っている場合、録画予約と重なると録画に失敗するため、再スキャンは録画のない時間帯を選んで手動実行するのが安全です。

### TSMF 分離フィルタ

`isdb-tsmf-split` は、TSMF 多重された TS ストリームから、指定した相対 TS を取り出す標準入出力フィルタです。  
**TSMF 分離に対応していないレコーダー (mirakc / EDCB など) でも、チューナーコマンドの後段にパイプで挟むことで CATV の多重チャンネルを選局できます。**

```bash
# 相対 TS 番号 1 を取り出す
dvbv5-zap -c dvbv5_channels_catv.conf -P -o - -t 30 CATV_15 | isdb-tsmf-split --rel-ts 1 > output.ts

# 多重されている相対 TS の一覧とキャリア種別を JSON で確認
cat multiplexed.ts | isdb-tsmf-split --list
```

### 8K マルチキャリアの並列収録

`isdb-catv-capture` は、複数のチューナーを使って複数の物理チャンネルを **同時に** 収録するコマンドです。  
8K 放送が複数の物理チャンネルに分割して伝送されている場合、それらを同時に収録することで、後から結合 (再多重) して 8K を復元できる可能性のあるデータを取得できます。

```bash
# 8K を構成する 3 キャリア (物理チャンネルはスキャン結果の 8K マルチキャリアグループ情報で確認) を
# 3 つのチューナーで同時に 30 秒間収録
poetry run isdb-catv-capture --channels CATV_C32,CATV_C33,CATV_C34 --time 30 --output-dir ./captured/
```

主なオプション:

- **`--channels <物理チャンネル,...>`** (必須): 同時に収録する物理チャンネルをカンマ区切りで指定します。
- **`--time <秒>`**: 各チャンネルの収録時間 (秒) を指定します (デフォルト: 30 秒)。
- **`--output-dir <ディレクトリ>`**: 収録した TS ファイルの出力先を指定します (デフォルト: `./captured/`)。
- **`--adapters <番号,...>`**: 使用する DVB アダプタ番号をチャンネルごとにカンマ区切りで指定します。省略時は検出されたチューナーから自動割り当てします。

各チューナーの選局開始タイミングを揃えて同時収録し、収録後に各ファイルの MMTP シーケンス番号の範囲が重複しているか (＝分散伝送を再結合できるデータか) を判定して表示します。

> [!IMPORTANT]  
> 同時収録するチャンネル数だけ、DVB-C 対応チューナーが必要です。チャンネル数がチューナー数を超える場合はエラーになります。

## 注意事項

- **すでに Mirakurun / mirakc を導入している環境でチャンネルスキャンを行う際は、できるだけ Mirakurun / mirakc を停止してから行ってください。**
  - ISDBScanner は Mirakurun / mirakc を経由せず、recisdb を通してダイレクトにチューナーデバイスにアクセスします。  
    **チャンネルスキャンと Mirakurun / mirakc による EPG 更新や録画のタイミングが重なると、チューナー数次第ではチューナーが不足してスキャンに失敗する可能性があります。**
  - 録画中でないことを確認の上一旦 Mirakurun / mirakc サービスを停止し、ほかのソフトにチューナーを横取りされない状況でスキャンすることをおすすめします。  
    スキャン完了後は停止した Mirakurun / mirakc サービスの再開を忘れずに。
- **EDCB-Wine のチャンネル設定ファイルを実稼働環境に反映する場合は、EDCB-Wine で利用している Mirakurun / mirakc のチャンネル設定ファイルも、必ず同時に更新してください。**
  - BonDriver は物理チャンネル自体の数値ではなく基本 0 からの連番となる「通し番号」でチャンネル切り替えを行う仕様になっていて、ChSet4.txt にはこの通し番号が記載されています。
    - EDCB-Wine で利用している BonDriver_mirakc の場合、Mirakurun / mirakc 側で登録した物理チャンネルの配列インデックスがそのまま「通し番号」になります。
  - つまり、**Mirakurun / mirakc のチャンネル設定ファイルを変更して登録中の物理チャンネルを増減させると、この BonDriver の「通し番号」がズレてしまい、再度 ChSet4.txt を生成し直さない限り正しくチャンネル切り替えが行えない状態に陥ります。**
    - 実際私はこれが原因で録画に失敗したことがあります…。
    - こうした事態を避けるため、**EDCB-Wine と Mirakurun / mirakc のチャンネル設定ファイルは、片方だけを更新するのではなく、常に両方を同時に更新するようにしてください。**
- **深夜にチャンネルスキャンを行うと、停波中のチャンネルがスキャン結果から漏れてしまいます。**
  - 特に NHK Eテレは毎日深夜に放送を休止しているため、深夜にスキャンを行うとスキャン結果から漏れてしまいます。
  - できるだけ (停波中のチャンネルがない) 日中時間帯でのチャンネルスキャンをおすすめします。

## License

[MIT License](License.txt)
