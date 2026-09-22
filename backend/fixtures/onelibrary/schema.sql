CREATE TABLE IF NOT EXISTS "content" (
    "content_id"    integer primary key not null,
    "title" varchar,
    "titleForSearch"    varchar,
    "subtitle"  varchar,
    "bpmx100"   int,
    "length"    int,
    "trackNo"   int,
    "discNo"    int,
    "artist_id_artist"  int,
    "artist_id_remixer" int,
    "artist_id_originalArtist"  int,
    "artist_id_composer"    int,
    "artist_id_lyricist"    int,
    "album_id"  int,
    "genre_id"  int,
    "label_id"  int,
    "key_id"    int,
    "color_id"  int,
    "image_id"  int,
    "djComment" varchar,
    "rating"    int,
    "releaseYear"   int,
    "releaseDate"   varchar,
    "dateAdded" varchar,
    "dateCreated"   varchar,
    "path"  varchar,
    "fileName"  varchar,
    "fileSize"  int,
    "fileType"  int,
    "bitrate"   int,
    "bitDepth"  int,
    "samplingRate"  int,
    "isrc"  varchar,
    "djPlayCount"   int,
    "isHotCueAutoLoadOn"    int,
    "isKuvoDeliverStatusOn" int,
    "kuvoDeliveryComment"   varchar,
    "masterDbId"    int not null,
    "masterContentId"   int not null,
    "analysisDataFilePath"  varchar,
    "analysedBits"  int,
    "contentLink"   int,
    "hasModified"   int,
    "cueUpdateCount"    int,
    "analysisDataUpdateCount"   int,
    "informationUpdateCount"    int
);

CREATE TABLE IF NOT EXISTS "genre" (
    "genre_id"  integer primary key not null,
    "name"  varchar
);

CREATE TABLE IF NOT EXISTS "artist" (
    "artist_id" integer primary key not null,
    "name"  varchar,
    "nameForSearch" varchar
);

CREATE TABLE IF NOT EXISTS "album" (
    "album_id"  integer primary key not null,
    "name"  varchar not null,
    "artist_id" int,
    "image_id"  int,
    "isComplation" int,
    "nameForSearch" varchar
);

CREATE TABLE IF NOT EXISTS "label" (
    "label_id"  integer primary key not null,
    "name"  varchar
);

CREATE TABLE IF NOT EXISTS "key" (
    "key_id"    integer primary key not null,
    "name"  varchar
);

CREATE TABLE IF NOT EXISTS "color" (
    "color_id"  integer primary key not null,
    "name"  varchar
);

CREATE TABLE IF NOT EXISTS "playlist" (
    "playlist_id"   integer primary key not null,
    "sequenceNo"    int,
    "name"  varchar,
    "image_id"  int,
    "attribute" int,
    "playlist_id_parent"    int
);

CREATE TABLE IF NOT EXISTS "playlist_content" (
    "playlist_id"   int not null,
    "content_id"    int not null,
    "sequenceNo"    int not null
);

CREATE TABLE IF NOT EXISTS "history" (
    "history_id"    integer primary key not null,
    "sequenceNo"    int,
    "name"  varchar,
    "attribute" int,
    "history_id_parent" int
);

CREATE TABLE IF NOT EXISTS "history_content" (
    "history_id"    int,
    "content_id"    int,
    "sequenceNo"    int
);

CREATE TABLE IF NOT EXISTS "image" (
    "image_id"  integer primary key not null,
    "path"  varchar
);

CREATE TABLE IF NOT EXISTS "menuItem" (
    "menuItem_id"   integer primary key not null,
    "kind"  int,
    "name"  varchar
);

CREATE TABLE IF NOT EXISTS "category" (
    "category_id"   integer primary key not null,
    "menuItem_id"   int,
    "sequenceNo"    int,
    "isVisible" int
);

CREATE TABLE IF NOT EXISTS "sort" (
    "sort_id"   integer primary key not null,
    "menuItem_id"   int,
    "sequenceNo"    int,
    "isVisible" int,
    "isSelectedAsSubColumn" int
);

CREATE TABLE IF NOT EXISTS "property" (
    "deviceName"    varchar,
    "dbVersion" varchar,
    "numberOfContents"  int,
    "createdDate"   varchar,
    "backGroundColorType"   int,
    "myTagMasterDBID"   int
);

CREATE TABLE IF NOT EXISTS "hotCueBankList" (
    "hotCueBankList_id" integer primary key not null,
    "sequenceNo"    int,
    "name"  varchar,
    "image_id"  int,
    "attribute" int,
    "hotCueBankList_id_parent"  int
);

CREATE TABLE IF NOT EXISTS "hotCueBankList_cue" (
    "hotCueBankList_id" int not null,
    "cue_id"    int not null,
    "sequenceNo"    int not null
);

CREATE TABLE IF NOT EXISTS "cue" (
    "cue_id"    integer primary key not null,
    "content_id"    int not null,
    "kind"  int not null,
    "colorTableIndex" int,
    "cueComment" varchar,
    "isActiveLoop" int not null,
    "beatLoopNumerator" int,
    "beatLoopDenominator" int,
    "inUsec" int not null,
    "outUsec" int,
    "in150FramePerSec" int not null,
    "out150FramePerSec" int,
    "inMpegFrameNumber" int,
    "outMpegFrameNumber" int,
    "inMpegAbs" int,
    "outMpegAbs" int,
    "inDecodingStartFramePosition" int,
    "outDecodingStartFramePosition" int,
    "inFileOffsetInBlock" int,
    "outFileOffsetInBlock" int,
    "inNumberOfSampleInBlock" int,
    "outNumberOfSampleInBlock" int
);

CREATE TABLE IF NOT EXISTS "recommendedLike" (
    "content_id_1"  int not null,
    "content_id_2"  int not null,
    "rating"    int,
    "createdDate"   datetime
);

CREATE TABLE IF NOT EXISTS "myTag" (
    "myTag_id"  integer primary key not null,
    "sequenceNo"    int,
    "name"  varchar,
    "attribute" int,
    "myTag_id_parent" int
);

CREATE TABLE IF NOT EXISTS "myTag_content" (
    "myTag_id"  int not null,
    "content_id"    int not null
);

